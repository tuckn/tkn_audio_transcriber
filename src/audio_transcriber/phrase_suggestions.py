"""Generate grounded vocabulary candidates, then stop for human review."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .config import ResolvedConfig
from .errors import AudioTranscriberError, ConfigError, ValidationError
from .io_utils import atomic_write_json, atomic_write_text, sha256_file
from .phrase_files import validate_phrase

MAX_INPUT_BYTES = 4_000_000
MAX_PROMPT_CHARACTERS = 120_000


def _read_input(path: Path) -> tuple[str, str]:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_INPUT_BYTES + 1)
        if len(content) > MAX_INPUT_BYTES:
            raise ValidationError("Suggestion input exceeds the 4 MB limit; split it first")
        return content.decode("utf-8-sig"), hashlib.sha256(content).hexdigest()
    except (OSError, UnicodeError) as exc:
        raise ValidationError("Cannot read suggestion input as UTF-8") from exc


def _read_segments(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    indices: set[int] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError
            index, start, end, utterance = (item["index"], item["start"], item["end"], item["text"])
            if (
                type(index) is not int
                or index < 1
                or index in indices
                or type(start) not in (int, float)
                or type(end) not in (int, float)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end
                or not isinstance(utterance, str)
                or not utterance.strip()
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ValidationError(
                f"Invalid transcript JSONL segment at line {line_number}"
            ) from None
        indices.add(index)
        segments.append({"index": index, "start": start, "end": end, "text": utterance})
    if not segments:
        raise ValidationError("Transcript contains no non-empty segments")
    return segments


def _generation_selection(config: ResolvedConfig, selector: str | None) -> tuple[str, str, int]:
    generation = config.generation
    name = selector or generation["active_profile"]
    settings = generation["profiles"].get(name)
    if not isinstance(settings, dict) or not settings.get("bridge_profile"):
        raise ConfigError(f"Unknown or incomplete generation profile: {name}")
    return name, str(settings["bridge_profile"]), int(generation["max_candidates"])


def _schema(max_candidates: int) -> dict[str, Any]:
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"segment_index": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["segment_index", "quote"],
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "term": {"type": "string"},
            "kind": {"type": "string", "enum": ["extracted", "inferred_correction"]},
            "reason": {"type": "string"},
            "evidence": {"type": "array", "items": evidence, "minItems": 1, "maxItems": 3},
        },
        "required": ["term", "kind", "reason", "evidence"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "candidates": {"type": "array", "items": candidate, "maxItems": max_candidates}
        },
        "required": ["candidates"],
    }


def _validate_candidates(
    data: dict[str, Any], segments: list[dict[str, Any]], maximum: int
) -> list[dict[str, Any]]:
    """Validate source evidence as well as the Bridge's structural validation."""
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > maximum:
        raise ValidationError("Generated candidates exceed the limit or have an invalid structure")
    by_index = {item["index"]: item for item in segments}
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            term = validate_phrase(candidate["term"])
            kind, reason, evidence = candidate["kind"], candidate["reason"], candidate["evidence"]
            if (
                term.startswith("#")
                or len(term) > 200
                or kind not in {"extracted", "inferred_correction"}
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 2000
                or not isinstance(evidence, list)
                or not 1 <= len(evidence) <= 3
            ):
                raise ValueError
            verified: list[dict[str, Any]] = []
            for reference in evidence:
                index, quote = reference["segment_index"], reference["quote"]
                if type(index) is not int or index not in by_index:
                    raise ValueError
                source = by_index[index]
                if not isinstance(quote, str) or not quote.strip() or quote not in source["text"]:
                    raise ValueError
                verified.append(
                    {
                        "segment_index": index,
                        "quote": quote,
                        "start": source["start"],
                        "end": source["end"],
                    }
                )
            if kind == "extracted" and not any(term in item["quote"] for item in verified):
                raise ValueError
        except (KeyError, TypeError, ValueError, ConfigError):
            raise ValidationError(
                "Generated candidate evidence does not match the transcript; "
                "no review files were saved. Retry or use a different generation profile."
            ) from None
        if term not in seen:
            seen.add(term)
            result.append({"term": term, "kind": kind, "reason": reason, "evidence": verified})
    return result


def _markdown(value: str) -> str:
    value = html.escape(value).replace("\r", " ").replace("\n", " ")
    for char in ("\\", "`", "*", "_", "[", "]", "|", "#", "~"):
        value = value.replace(char, "\\" + char)
    return value


def _timestamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02}:{whole // 60 % 60:02}:{whole % 60:02}"


def _review(candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# 用語候補の確認",
        "",
        "`phrases.txt` の表記を修正し、不要な用語は行ごと削除してください。",
        "確認後、そのファイルを `transcribe --phrase-file` に指定してください。",
        "時刻は引用元セグメントの範囲です。推測した訂正は、必要に応じて音声で確認してください。",
        "候補生成だけでは再文字起こしは実行されません。",
        "",
    ]
    if not candidates:
        lines.append("登録を提案する用語はありませんでした。必要な用語を手動で追加できます。")
    for candidate in candidates:
        kind = "原文から抽出" if candidate["kind"] == "extracted" else "訂正の推測・要確認"
        lines.extend(
            [
                f"## {_markdown(candidate['term'])}",
                "",
                f"- 種別: {kind}",
                f"- 理由: {_markdown(candidate['reason'])}",
            ]
        )
        for reference in candidate["evidence"]:
            lines.append(
                f"- {_timestamp(reference['start'])}–{_timestamp(reference['end'])} "
                f"（segment {reference['segment_index']}）: {_markdown(reference['quote'])}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def suggest_phrases(
    transcript: Path,
    *,
    output_dir: Path,
    config: ResolvedConfig,
    generation_profile: str | None = None,
    context_file: Path | None = None,
    max_candidates: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    target = output_dir.expanduser().resolve()
    # Never replace even an empty review directory, or call the provider before this check.
    if target.exists():
        raise ValidationError("Suggestion output directory already exists; choose a new directory")
    source_text, source_hash = _read_input(transcript.expanduser())
    segments = _read_segments(source_text)
    context, context_hash = _read_input(context_file.expanduser()) if context_file else ("", None)
    name, bridge_profile, default_maximum = _generation_selection(config, generation_profile)
    maximum = default_maximum if max_candidates is None else max_candidates
    if type(maximum) is not int or not 1 <= maximum <= 500:
        raise ConfigError("max_candidates must be an integer between 1 and 500")
    instructions = (
        files("audio_transcriber")
        .joinpath("resources/phrase-suggestions.txt")
        .read_text(encoding="utf-8")
    )
    prompt = (
        instructions
        + "\n"
        + json.dumps(
            {"max_candidates": maximum, "context": context, "segments": segments},
            ensure_ascii=False,
        )
    )
    if len(prompt) > MAX_PROMPT_CHARACTERS:
        raise ValidationError("Suggestion prompt exceeds 120000 characters; split the JSONL first")

    # Import lazily so ordinary transcription does not initialize the generation stack.
    from tkn_genai_bridge import GenAIError, GenerationRequest, Runtime, load_profile

    request = GenerationRequest(
        prompt=prompt, output_schema=_schema(maximum), schema_name="transcription_phrase_candidates"
    )
    output_names = ("phrases.txt", "review.md", "candidates.json", "manifest.json")
    payload: dict[str, Any] = {
        "status": "planned" if dry_run else "review_required",
        "generation_profile": name,
        "bridge_profile": bridge_profile,
        "source_sha256": source_hash,
        "context_sha256": context_hash,
        "segment_count": len(segments),
        "max_candidates": maximum,
        "outputs": {item: str(target / item) for item in output_names},
    }
    try:
        with Runtime(load_profile(bridge_profile)) as runtime:
            if dry_run:
                payload["plan"] = runtime.plan(request).model_dump(mode="json")
                payload["network_calls"] = 0
                return payload
            generated = runtime.generate(request)
    except GenAIError as exc:
        # Provider error messages can contain private response text; keep diagnostics bounded.
        code = getattr(exc, "code", "generation_failed")
        raise AudioTranscriberError(
            f"Phrase generation failed ({code}); check Bridge settings"
        ) from None
    candidates = _validate_candidates(generated.data, segments, maximum)
    phrases = "# 未確認の用語候補です。修正・削除してから --phrase-file に指定してください。\n"
    phrases += "\n".join(item["term"] for item in candidates) + "\n"
    payload["candidate_count"] = len(candidates)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".phrase-suggestions-", dir=target.parent) as temporary:
        stage = Path(temporary) / "review"
        stage.mkdir()
        atomic_write_text(stage / "phrases.txt", phrases)
        atomic_write_text(stage / "review.md", _review(candidates))
        atomic_write_json(stage / "candidates.json", {"candidates": candidates})
        atomic_write_json(
            stage / "manifest.json",
            {
                "schema_version": "1.0.0",
                "app_version": __version__,
                "status": "review_required",
                "source_sha256": source_hash,
                "context_sha256": context_hash,
                "generation_profile": name,
                "bridge_profile": bridge_profile,
                "candidate_count": len(candidates),
                "generation": generated.record.model_dump(mode="json"),
                "outputs": {
                    item: {"sha256": sha256_file(stage / item)} for item in output_names[:-1]
                },
            },
        )
        if target.exists():
            raise ValidationError(
                "Suggestion output appeared during generation; existing files kept"
            )
        os.rename(stage, target)
    return payload

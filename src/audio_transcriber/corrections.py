"""Correct saved transcripts with generative AI, one grounded word-level replacement at a time.

The transcript JSONL is never changed: it stays the recognizer's original text. Markdown,
text, and SRT are rendered again from the JSONL with the applied replacements, and every
run is kept in `<transcript>.corrections.json`, so `revert` can restore the original files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .config import ResolvedConfig
from .errors import AudioTranscriberError, ValidationError
from .genai import connection, require_provider_executable, selected_connection_name
from .io_utils import atomic_write_json, atomic_write_text, sha256_file
from .models import Segment
from .pipeline import parse_segments
from .rendering import markdown_transcript_lines, render_srt, render_text
from .validation import validate_artifact

CORRECTIONS_SCHEMA_VERSION = "1.0.0"
CORRECTIONS_SUFFIX = ".corrections.json"
KINDS = ("proper_noun", "term", "notation", "misrecognition")
MAX_CONTEXT_FILE_BYTES = 1_000_000
MAX_CONTEXT_CHARACTERS = 60_000
# Segments per request are limited so a long recording is corrected in several requests.
MAX_BATCH_CHARACTERS = 40_000
MIN_BATCH_CHARACTERS = 2_000
MAX_PROMPT_CHARACTERS = 120_000
MAX_CHANGES_PER_REQUEST = 300
MAX_BEFORE_CHARACTERS = 50
MAX_AFTER_CHARACTERS = 80
MAX_REASON_CHARACTERS = 300
_REJECTED_TEXT_CHARACTERS = 100
_TRANSCRIPT_MARKER = "\n## Transcript\n\n"
_MARKDOWN_KEY = "corrections"
_RENDERED = ("markdown", "text", "srt")


@dataclass(frozen=True)
class TranscriptFiles:
    jsonl: Path
    manifest: Path
    markdown: Path
    text: Path
    srt: Path
    corrections: Path


def corrections_path(jsonl: Path) -> Path:
    name = jsonl.name.removesuffix(".jsonl")
    return jsonl.with_name(f"{name}{CORRECTIONS_SUFFIX}")


_CHANGED = (
    "The transcript files changed after they were written or are incomplete, so they are "
    "not corrected or reverted; restore them, or run transcribe --overwrite to write them again."
)


def _transcript_files(
    transcript: Path,
) -> tuple[TranscriptFiles, dict[str, Any], dict[Path, bytes | None]]:
    """The transcript's own files, its manifest, and their bytes checked against the manifest.

    The returned snapshot is what the correction starts from; publishing compares it again.
    """
    jsonl = transcript.expanduser().resolve()
    if jsonl.suffix != ".jsonl" or not jsonl.is_file():
        raise ValidationError(
            f"Transcript JSONL not found: {jsonl}; pass the transcript_*.jsonl this CLI saved",
            target_path=jsonl,
        )
    stem = jsonl.name.removesuffix(".jsonl")
    # Only this transcript's own files are ever rewritten, whatever the manifest points to.
    transcript_files = TranscriptFiles(
        jsonl=jsonl, manifest=jsonl.with_name(f"{stem}.manifest.json"),
        markdown=jsonl.with_name(f"{stem}.md"), text=jsonl.with_name(f"{stem}.txt"),
        srt=jsonl.with_name(f"{stem}.srt"), corrections=corrections_path(jsonl),
    )
    if not transcript_files.manifest.is_file():
        raise ValidationError(f"Transcript manifest not found: {transcript_files.manifest}")
    try:
        # Also checks the shared audio and the source record.
        validate_artifact(transcript_files.manifest, verify_source=False)
    except ValidationError as exc:
        raise ValidationError(f"{exc}. {_CHANGED}", target_path=exc.target_path) from None
    snapshot = _snapshot(transcript_files)
    manifest_bytes = snapshot[transcript_files.manifest]
    try:
        manifest = json.loads(b"" if manifest_bytes is None else manifest_bytes)
    except ValueError:
        raise ValidationError(f"Cannot read manifest {transcript_files.manifest}") from None
    outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
    if not isinstance(outputs, dict) or not all(
        isinstance(outputs.get(label), dict) for label in (*_RENDERED, "jsonl")
    ):
        raise ValidationError(
            "Manifest does not list markdown, text, srt, and jsonl outputs; transcripts from "
            "versions before 0.16.0 have no text output, so transcribe them again first"
        )
    owned = {
        "jsonl": transcript_files.jsonl, "markdown": transcript_files.markdown,
        "text": transcript_files.text, "srt": transcript_files.srt,
    }
    if "corrections" in outputs:
        owned["corrections"] = transcript_files.corrections
    for label, path in owned.items():
        record = outputs.get(label)
        if not isinstance(record, dict) or record.get("path") != path.name:
            raise ValidationError(
                f"Manifest outputs are not this transcript's own files: {transcript_files.manifest}"
            )
        # The bytes the correction starts from are the ones the manifest describes.
        content = snapshot[path]
        if content is None or hashlib.sha256(content).hexdigest() != record.get("sha256"):
            raise ValidationError(f"Output hash does not match manifest: {path}. {_CHANGED}")
    return transcript_files, manifest, snapshot


def _snapshot_segments(
    transcript_files: TranscriptFiles, snapshot: dict[Path, bytes | None]
) -> list[Segment]:
    content = snapshot[transcript_files.jsonl]
    assert content is not None
    return parse_segments(content.decode("utf-8"), transcript_files.jsonl)


def _empty_log(jsonl: Path) -> dict[str, Any]:
    return {"schema_version": CORRECTIONS_SCHEMA_VERSION, "transcript": jsonl.name, "runs": []}


def _read_log(
    transcript_files: TranscriptFiles, snapshot: dict[Path, bytes | None]
) -> dict[str, Any]:
    path = transcript_files.corrections
    content = snapshot[path]
    if content is None:
        return _empty_log(transcript_files.jsonl)
    try:
        log = json.loads(content.decode("utf-8"))
    except ValueError as exc:
        raise ValidationError(f"Cannot read corrections log {path}: {exc}") from None
    if not isinstance(log, dict):
        raise ValidationError(f"Unsupported corrections log: {path}")
    runs = log.get("runs")
    if log.get("schema_version") != CORRECTIONS_SCHEMA_VERSION or not isinstance(runs, list):
        raise ValidationError(f"Unsupported corrections log: {path}")
    if sum(1 for run in runs if isinstance(run, dict) and run.get("status") == "applied") > 1:
        raise ValidationError(f"Corrections log has more than one applied run: {path}")
    return dict(log)


def _applied_run(log: dict[str, Any]) -> dict[str, Any] | None:
    for run in log["runs"]:
        if run.get("status") == "applied":
            return dict(run)
    return None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_context(paths: Sequence[Path]) -> tuple[str, list[dict[str, Any]]]:
    texts: list[str] = []
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = raw.expanduser().resolve()
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_CONTEXT_FILE_BYTES + 1)
        except OSError as exc:
            raise ValidationError(
                f"Cannot read context file {path}: {exc}", target_path=path
            ) from None
        if len(content) > MAX_CONTEXT_FILE_BYTES:
            raise ValidationError(f"Context file exceeds 1 MB: {path}", target_path=path)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError:
            raise ValidationError(f"Context file is not UTF-8: {path}", target_path=path) from None
        texts.append(f"--- {path.name} ---\n{text.strip()}")
        records.append({
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "characters": len(text),
        })
    context = "\n\n".join(texts)
    if len(context) > MAX_CONTEXT_CHARACTERS:
        raise ValidationError(
            f"Correction context exceeds {MAX_CONTEXT_CHARACTERS} characters; shorten it"
        )
    return context, records


def preflight_correction(
    config: ResolvedConfig, *, genai_profile: str | None = None,
    context_files: Sequence[Path] = (),
) -> dict[str, Any]:
    """Check the connection and context offline before paying for a transcription."""
    name = selected_connection_name(config, genai_profile)
    profile = connection(config, name)
    require_provider_executable(profile, name)
    context, records = _read_context(context_files)
    _segment_budget(_instructions(), context)
    return {
        "genai_profile": name, "provider": profile.provider, "model": profile.model,
        "local_only": profile.local_only, "context_files": records,
    }


def _schema() -> dict[str, Any]:
    change = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "segment_index": {"type": "integer"},
            "before": {"type": "string"},
            "after": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "reason": {"type": "string"},
        },
        "required": ["segment_index", "before", "after", "kind", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "changes": {"type": "array", "maxItems": MAX_CHANGES_PER_REQUEST, "items": change}
        },
        "required": ["changes"],
    }


def _instructions() -> str:
    return (
        files("audio_transcriber")
        .joinpath("resources/transcript-correction.txt")
        .read_text(encoding="utf-8")
    )


def _segment_budget(instructions: str, context: str) -> int:
    """Characters of segment JSON per request, after the instructions and the context."""
    fixed = len(instructions) + 1 + len(
        json.dumps({"context": context, "segments": []}, ensure_ascii=False)
    )
    room = MAX_PROMPT_CHARACTERS - fixed
    if room < MIN_BATCH_CHARACTERS:
        raise ValidationError(
            "The correction context is too long to leave room for the transcript; shorten it"
        )
    return min(MAX_BATCH_CHARACTERS, room)


def _batches(segments: list[Segment], budget: int) -> list[list[dict[str, Any]]]:
    """Split segments so their JSON stays within `budget` characters per request."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for segment in segments:
        item: dict[str, Any] = {"index": segment.index, "text": segment.text}
        if segment.speaker is not None:
            item["speaker"] = segment.speaker
        length = len(json.dumps(item, ensure_ascii=False)) + 2
        if current and size + length > budget:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        batches.append(current)
    return batches


def _clip(value: object) -> object:
    """A short, UTF-8-safe copy of a rejected value for the log."""
    if isinstance(value, str):
        value = value.encode("utf-8", "replace").decode("utf-8")
        if len(value) > _REJECTED_TEXT_CHARACTERS:
            return value[:_REJECTED_TEXT_CHARACTERS] + "…"
        return value
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _ascii_word_character(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def _occurrences(text: str, before: str) -> list[int]:
    """Offsets of `before` in `text`, except inside a longer English word or number."""
    offsets: list[int] = []
    start = text.find(before)
    while start != -1:
        end = start + len(before)
        inside = (
            _ascii_word_character(before[0]) and start > 0
            and _ascii_word_character(text[start - 1])
        ) or (
            _ascii_word_character(before[-1]) and end < len(text)
            and _ascii_word_character(text[end])
        )
        if inside:
            # "Net" in "Netflix" is a different word; Japanese text has no such boundary.
            start = text.find(before, start + 1)
        else:
            offsets.append(start)
            start = text.find(before, end)
    return offsets


def _validate_changes(
    data: dict[str, Any],
    batch_indices: set[int],
    by_index: dict[int, Segment],
    occupied: dict[int, list[tuple[int, int]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only replacements whose original text is really in that segment."""
    changes = data.get("changes")
    if not isinstance(changes, list):
        raise ValidationError("Generated corrections have an invalid structure")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in changes:
        item = raw if isinstance(raw, dict) else {}
        index, before, after = item.get("segment_index"), item.get("before"), item.get("after")
        kind, reason = item.get("kind"), item.get("reason")
        problem = _change_problem(index, before, after, kind, reason, batch_indices)
        offsets: list[int] = []
        ranges: list[tuple[int, int]] = []
        if problem is None:
            assert isinstance(index, int) and isinstance(before, str)
            offsets = _occurrences(by_index[index].text, before)
            ranges = [(offset, offset + len(before)) for offset in offsets]
            taken = occupied.setdefault(index, [])
            if not offsets:
                problem = "inside_word" if before in by_index[index].text else "not_found"
            elif any(
                start < end2 and start2 < end for start, end in ranges for start2, end2 in taken
            ):
                problem = "overlap"
        if problem is not None:
            rejected.append({
                "segment_index": index if type(index) is int else None,
                "before": _clip(before), "after": _clip(after), "reason": problem,
            })
            continue
        assert isinstance(index, int) and isinstance(reason, str)
        occupied[index].extend(ranges)
        segment = by_index[index]
        accepted.append({
            "segment_index": index, "start": segment.start, "end": segment.end,
            "before": before, "after": after, "kind": kind, "reason": reason.strip(),
            "offsets": offsets,
        })
    return accepted, rejected


def _change_problem(
    index: object, before: object, after: object, kind: object, reason: object,
    batch_indices: set[int],
) -> str | None:
    """Why one generated replacement cannot be used, before looking at the segment text."""
    if (
        type(index) is not int or not isinstance(before, str) or not isinstance(after, str)
        or kind not in KINDS or not isinstance(reason, str)
    ):
        return "invalid"
    try:
        for value in (before, after, reason):
            value.encode("utf-8")
    except UnicodeEncodeError:
        # For example, a lone surrogate written as an escape in the model's JSON.
        return "invalid_text"
    if index not in batch_indices:
        return "unknown_segment"
    if not before.strip() or not after.strip() or before == after:
        return "same_or_empty"
    if any(char in value for value in (before, after, reason) for char in "\r\n\t"):
        return "multiline"
    if (
        len(before) > MAX_BEFORE_CHARACTERS or len(after) > MAX_AFTER_CHARACTERS
        or len(reason) > MAX_REASON_CHARACTERS
    ):
        return "too_long"
    return None


def _apply(segments: list[Segment], changes: list[dict[str, Any]]) -> list[Segment]:
    edits: dict[int, list[tuple[int, str, str]]] = {}
    for change in changes:
        for offset in change["offsets"]:
            edits.setdefault(change["segment_index"], []).append(
                (offset, change["before"], change["after"])
            )
    corrected: list[Segment] = []
    for segment in segments:
        text = segment.text
        # From the end, so earlier offsets still point into the original text.
        for start, before, after in sorted(edits.get(segment.index, []), reverse=True):
            if text[start:start + len(before)] != before:
                raise ValidationError("Correction offsets do not match the transcript")
            text = text[:start] + after + text[start + len(before):]
        corrected.append(replace(segment, text=text))
    return corrected


def _rewrite_markdown(text: str, segments: list[Segment], log_name: str | None) -> str:
    """Keep the Markdown front matter and heading; render the transcript section again."""
    head, marker, _body = text.partition(_TRANSCRIPT_MARKER)
    lines = head.split("\n")
    if not marker or not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValidationError("Markdown transcript does not have the expected layout")
    end = lines.index("---", 1)
    front = [line for line in lines[1:end] if not line.startswith(f"{_MARKDOWN_KEY}: ")]
    if log_name is not None:
        front.append(f"{_MARKDOWN_KEY}: {json.dumps(log_name, ensure_ascii=False)}")
    body = "".join(f"{line}\n" for line in markdown_transcript_lines(segments))
    return "\n".join(["---", *front, *lines[end:]]) + marker + body


def _record(path: Path, published: Path | None = None) -> dict[str, Any]:
    """Manifest record of `path`, named as the file it will be published as."""
    return {
        "path": (published or path).name, "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _snapshot(transcript_files: TranscriptFiles) -> dict[Path, bytes | None]:
    """Current bytes of the JSONL and of every file a commit replaces (None if absent)."""
    paths = (
        transcript_files.jsonl, transcript_files.markdown, transcript_files.text,
        transcript_files.srt, transcript_files.corrections, transcript_files.manifest,
    )
    return {path: path.read_bytes() if path.exists() else None for path in paths}


@contextmanager
def publish_lock(jsonl: Path) -> Iterator[None]:
    """Let one process at a time publish the files of one transcript.

    Both correction and the transcription pipeline take it, so neither replaces files
    while the other is between its check and its manifest.
    """
    path = jsonl.with_name(f".{jsonl.name.removesuffix('.jsonl')}.corrections.lock")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValidationError(
            f"Another run is writing this transcript ({path} exists). If none is "
            "running, delete that file and run the command again."
        ) from None
    os.close(descriptor)
    try:
        yield
    finally:
        with suppress(OSError):
            path.unlink()


def _commit(
    transcript_files: TranscriptFiles,
    manifest: dict[str, Any],
    log: dict[str, Any],
    segments: list[Segment],
    *,
    applied: dict[str, Any] | None,
    snapshot: dict[Path, bytes | None],
) -> None:
    """Write the rendered files, the log, and the manifest; the JSONL is never touched.

    Every file is staged first. If a file cannot be replaced, the ones already replaced
    are written back, so the transcript stays valid in its earlier state.
    """
    with publish_lock(transcript_files.jsonl):
        _publish(transcript_files, manifest, log, segments, applied=applied, snapshot=snapshot)
    validate_artifact(transcript_files.manifest, verify_source=False)


def _publish(
    transcript_files: TranscriptFiles,
    manifest: dict[str, Any],
    log: dict[str, Any],
    segments: list[Segment],
    *,
    applied: dict[str, Any] | None,
    snapshot: dict[Path, bytes | None],
) -> None:
    if _snapshot(transcript_files) != snapshot:
        # For example, the user edited a file while the model was answering.
        raise ValidationError(
            "The transcript files changed while the correction was being generated; "
            "nothing was written. Run the command again."
        )
    markdown = snapshot[transcript_files.markdown]
    assert markdown is not None
    contents = {
        transcript_files.markdown: _rewrite_markdown(
            markdown.decode("utf-8"), segments,
            None if applied is None else transcript_files.corrections.name,
        ),
        transcript_files.text: render_text(segments),
        transcript_files.srt: render_srt(segments),
    }
    token = secrets.token_hex(4)
    pending = {
        path: path.with_name(f".{path.name}.{token}.pending")
        for path in (*contents, transcript_files.corrections, transcript_files.manifest)
    }
    published: list[Path] = []
    try:
        for path, content in contents.items():
            atomic_write_text(pending[path], content)
        atomic_write_json(pending[transcript_files.corrections], log)
        outputs = dict(manifest["outputs"])
        for label, path in zip(_RENDERED, contents, strict=True):
            outputs[label] = _record(pending[path], path)
        outputs["corrections"] = _record(
            pending[transcript_files.corrections], transcript_files.corrections
        )
        atomic_write_json(
            pending[transcript_files.manifest],
            {
                **manifest,
                "outputs": outputs,
                "correction": None if applied is None else {
                    "run_id": applied["run_id"],
                    "applied_at": applied["created_at"],
                    "genai_profile": applied["genai_profile"],
                    "change_count": len(applied["changes"]),
                    "rejected_count": len(applied["rejected"]),
                },
            },
        )
        # The manifest goes last, so a stop before it leaves a hash mismatch, not a lie.
        for path, temporary in pending.items():
            os.replace(temporary, path)
            published.append(path)
    except BaseException:
        for path in reversed(published):
            original = snapshot[path]
            with suppress(OSError):
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_bytes(path, original)
        for temporary in pending.values():
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise


def _write_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.restore")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _outputs(transcript_files: TranscriptFiles) -> dict[str, str]:
    return {
        "markdown": str(transcript_files.markdown),
        "text": str(transcript_files.text),
        "srt": str(transcript_files.srt),
        "jsonl": str(transcript_files.jsonl),
        "manifest": str(transcript_files.manifest),
        "corrections": str(transcript_files.corrections),
    }


def correct_transcript(
    transcript: Path,
    *,
    config: ResolvedConfig,
    genai_profile: str | None = None,
    context_files: Sequence[Path] = (),
    dry_run: bool = False,
    redo: bool = False,
    if_needed: bool = False,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Correct the rendered transcript files from the original JSONL.

    `redo` corrects again from the original JSONL and supersedes the applied run.
    `if_needed` returns `unchanged` for an already corrected transcript instead of failing.
    """
    transcript_files, manifest, snapshot = _transcript_files(transcript)
    log = _read_log(transcript_files, snapshot)
    current = _applied_run(log)
    base = {"transcript": str(transcript_files.jsonl), "outputs": _outputs(transcript_files)}
    if current is not None and not redo:
        if if_needed:
            return {
                **base, "status": "unchanged", "reason": "already_corrected",
                "run_id": current["run_id"], "change_count": len(current["changes"]),
            }
        raise ValidationError(
            f"The transcript is already corrected (run {current['run_id']}); use --redo to "
            "correct it again from the original JSONL, or --revert to restore it"
        )
    segments = _snapshot_segments(transcript_files, snapshot)
    name = selected_connection_name(config, genai_profile)
    profile = connection(config, name)
    context, context_records = _read_context(context_files)
    instructions = _instructions()
    # Each request repeats the instructions and the context; segments fill the rest.
    batches = _batches(segments, _segment_budget(instructions, context))
    prompts = [
        instructions
        + "\n"
        + json.dumps({"context": context, "segments": batch}, ensure_ascii=False)
        for batch in batches
    ]
    if any(len(prompt) > MAX_PROMPT_CHARACTERS for prompt in prompts):
        raise ValidationError(
            f"A correction prompt exceeds {MAX_PROMPT_CHARACTERS} characters; shorten the context"
        )
    payload: dict[str, Any] = {
        **base,
        "status": "planned" if dry_run else "corrected",
        "genai_profile": name,
        "provider": profile.provider,
        "model": profile.model,
        "context_files": context_records,
        "segment_count": len(segments),
        "request_count": len(prompts),
    }
    if not segments:
        return {**payload, "status": "unchanged", "reason": "no_segments"}

    # Import lazily so ordinary transcription does not initialize the generation stack.
    from tkn_genai_bridge import GenAIError, GenerationRequest, Runtime

    requests = [
        GenerationRequest(
            prompt=prompt, output_schema=_schema(), schema_name="transcript_corrections"
        )
        for prompt in prompts
    ]
    by_index = {segment.index: segment for segment in segments}
    occupied: dict[int, list[tuple[int, int]]] = {}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    try:
        with Runtime(profile, profile_name=name) as runtime:
            if dry_run:
                payload["plans"] = [
                    runtime.plan(request).model_dump(mode="json") for request in requests
                ]
                payload["network_calls"] = 0
                return payload
            for number, (batch, request) in enumerate(zip(batches, requests, strict=True), 1):
                if logger is not None:
                    logger.info("Correcting transcript: request %d/%d", number, len(requests))
                generated = runtime.generate(request)
                records.append(generated.record.model_dump(mode="json"))
                kept, dropped = _validate_changes(
                    generated.data, {item["index"] for item in batch}, by_index, occupied
                )
                accepted.extend(kept)
                rejected.extend(dropped)
    except GenAIError as exc:
        # Provider error messages can contain private response text; keep diagnostics bounded.
        code = getattr(exc, "code", "generation_failed")
        raise AudioTranscriberError(
            f"Transcript correction failed ({code}); check genai_profiles.{name}. "
            "No transcript files were changed."
        ) from None
    moment = datetime.now().astimezone()
    created_at = moment.isoformat(timespec="seconds")
    run = {
        "run_id": f"{moment.strftime('%Y%m%dT%H%M%S%z')}-{secrets.token_hex(2)}",
        "status": "applied",
        "created_at": created_at,
        "tool_version": __version__,
        "base_jsonl_sha256": manifest["outputs"]["jsonl"]["sha256"],
        "genai_profile": name,
        "provider": profile.provider,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "context_files": context_records,
        "request_count": len(requests),
        "generation": records,
        "changes": accepted,
        "rejected": rejected,
    }
    runs = []
    for previous in log["runs"]:
        if previous.get("status") == "applied":
            previous = {
                **previous, "status": "superseded", "superseded_at": created_at,
                "superseded_by": run["run_id"],
            }
        runs.append(previous)
    log = {**log, "runs": [*runs, run]}
    _commit(
        transcript_files, manifest, log, _apply(segments, accepted), applied=run,
        snapshot=snapshot,
    )
    return {
        **payload,
        "run_id": run["run_id"],
        "change_count": len(accepted),
        "rejected_count": len(rejected),
        "changes": [
            {key: change[key] for key in ("segment_index", "before", "after", "kind")}
            for change in accepted
        ],
    }


def revert_corrections(transcript: Path) -> dict[str, Any]:
    """Render the original transcript again and mark the applied run as reverted."""
    transcript_files, manifest, snapshot = _transcript_files(transcript)
    log = _read_log(transcript_files, snapshot)
    current = _applied_run(log)
    base = {"transcript": str(transcript_files.jsonl), "outputs": _outputs(transcript_files)}
    if current is None:
        return {**base, "status": "unchanged", "reason": "not_corrected"}
    reverted_at = _now()
    log = {
        **log,
        "runs": [
            {**run, "status": "reverted", "reverted_at": reverted_at}
            if run.get("status") == "applied" else run
            for run in log["runs"]
        ],
    }
    _commit(
        transcript_files, manifest, log, _snapshot_segments(transcript_files, snapshot),
        applied=None,
        snapshot=snapshot,
    )
    return {
        **base, "status": "reverted", "run_id": current["run_id"],
        "change_count": len(current["changes"]),
    }


@dataclass(frozen=True)
class DiscardPlan:
    """The log of a transcript that is written again, with its applied run discarded."""

    path: Path
    log: dict[str, Any]
    # The log as it was, to put back if the transcript cannot be written after all.
    original: bytes

    def stage(self) -> Path:
        """Write the updated log next to the current one, for the caller to move in place."""
        pending = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.pending")
        atomic_write_json(pending, self.log)
        return pending

    def restore(self) -> None:
        """Put the earlier log back after a failed write of the transcript."""
        _write_bytes(self.path, self.original)

    def record(self) -> dict[str, Any]:
        """Manifest record of the log once it is in place."""
        return _record(self.path)


def plan_discard(jsonl: Path) -> DiscardPlan | None:
    """Read an existing log before a transcript is written again; None when there is none.

    An unreadable log stops the run, so it cannot later pass for an applied correction.
    """
    path = corrections_path(jsonl)
    if not path.exists():
        return None
    try:
        original = path.read_bytes()
        log = json.loads(original.decode("utf-8"))
        if not isinstance(log, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        raise ValidationError(
            f"Cannot read corrections log {path}: {exc}; the transcript was not written. "
            "Close the program that holds it or move it away, then run again."
        ) from None
    runs = log.get("runs")
    if not isinstance(runs, list):
        raise ValidationError(f"Unsupported corrections log {path}; the transcript was not written")
    discarded_at = _now()
    log["runs"] = [
        {**run, "status": "discarded", "discarded_at": discarded_at,
         "discard_reason": "transcript_written_again"}
        if isinstance(run, dict) and run.get("status") == "applied" else run
        for run in runs
    ]
    return DiscardPlan(path=path, log=log, original=original)

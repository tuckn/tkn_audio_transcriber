from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_pipeline import FakeFfmpeg, make_config, make_logger
from test_pull import _config_file, _pull, _recording
from tkn_genai_bridge import GenAIError, Profile, Runtime

from audio_transcriber import corrections
from audio_transcriber.cli import main
from audio_transcriber.config import ConfigError, ResolvedConfig, resolve_config
from audio_transcriber.corrections import (
    correct_transcript,
    corrections_path,
    revert_corrections,
)
from audio_transcriber.errors import AudioTranscriberError, ValidationError
from audio_transcriber.io_utils import read_json
from audio_transcriber.models import Segment, TranscriptionResult
from audio_transcriber.pipeline import TranscriptionPipeline
from audio_transcriber.validation import validate_artifact

TEXTS = {
    "chunk_000000.wav": "こんとそのアジュールで会議します",
    "chunk_000001.wav": "こんとそのチームとアジュールを使います",
}
CONTOSO = {
    "segment_index": 1, "before": "こんとそ", "after": "Contoso", "kind": "proper_noun",
    "reason": "資料にある社名",
}
AZURE = {
    "segment_index": 2, "before": "アジュール", "after": "Azure", "kind": "proper_noun",
    "reason": "製品名",
}


class WordRecognizer:
    def transcribe_chunk(
        self, chunk: Path, *, offset: float, first_index: int
    ) -> tuple[list[Segment], str, float]:
        segment = Segment(
            index=first_index, start=offset + 0.1, end=offset + 1.2,
            text=TEXTS[chunk.name], chunk=chunk.name,
        )
        return [segment], "ja", 0.99


def _pipeline(config: ResolvedConfig) -> TranscriptionPipeline:
    return TranscriptionPipeline(
        config=config, logger=make_logger(), ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda _model, _cfg: WordRecognizer(),
    )


def _transcribe(tmp_path: Path, *, overwrite: bool = False) -> TranscriptionResult:
    source = tmp_path / "meeting.wav"
    if not source.exists():
        source.write_bytes(b"meeting audio")
    return _pipeline(make_config(tmp_path)).transcribe(source, dry_run=False, overwrite=overwrite)


def _rendered(result: TranscriptionResult) -> dict[str, bytes]:
    outputs = result.outputs
    return {
        name: path.read_bytes()
        for name, path in (
            ("markdown", outputs.markdown), ("text", outputs.text),
            ("srt", outputs.srt), ("jsonl", outputs.jsonl),
        )
    }


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"requests": [], "profiles": [], "responses": []}

    def shared_bridge_config(*args: Any, **kwargs: Any) -> Profile:
        pytest.fail("the shared tkn-genai-bridge config must not be read")

    def generate(self: Runtime, request: Any) -> Any:
        state["requests"].append(request)
        state["profiles"].append((self.profile_name, self.profile))
        data = state["responses"].pop(0) if state["responses"] else {"changes": []}
        return SimpleNamespace(
            data=data,
            record=SimpleNamespace(
                model_dump=lambda **kwargs: {"provider": "codex", "status": "succeeded"}
            ),
        )

    monkeypatch.setattr("tkn_genai_bridge.load_profile", shared_bridge_config)
    monkeypatch.setattr("tkn_genai_bridge.load_config", shared_bridge_config)
    monkeypatch.setattr(Runtime, "generate", generate)
    # Tests must not depend on the provider CLIs installed on this computer.
    monkeypatch.setattr(
        "tkn_genai_bridge.providers.cli.resolve_executable", lambda profile: "codex"
    )
    return state


def test_correct_rewrites_rendered_files_and_keeps_the_original_jsonl(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("Contoso: 取引先の社名\nAzure\n", encoding="utf-8")
    bridge["responses"].append({"changes": [
        CONTOSO, AZURE,
        {**CONTOSO, "before": "存在しない語"},
        {**AZURE, "segment_index": 99},
        {**CONTOSO, "before": "とその", "after": "と、その"},
        {**AZURE, "after": "アジュール"},
        {**AZURE, "after": "Azure\nCloud"},
        {**AZURE, "before": "を使います" * 20},
        {**AZURE, "kind": "rewrite"},
    ]})
    corrected = correct_transcript(
        result.outputs.jsonl, config=make_config(tmp_path), context_files=[glossary]
    )

    assert corrected["status"] == "corrected"
    assert corrected["change_count"] == 2 and corrected["rejected_count"] == 7
    assert "取引先の社名" in bridge["requests"][0].prompt
    assert bridge["profiles"][0][0] == "codex-default"
    assert result.outputs.jsonl.read_bytes() == original["jsonl"]
    assert result.outputs.text.read_text(encoding="utf-8") == (
        "Contosoのアジュールで会議します\nこんとそのチームとAzureを使います\n"
    )
    assert "Contosoのアジュール" in result.outputs.srt.read_text(encoding="utf-8")
    markdown = result.outputs.markdown.read_text(encoding="utf-8")
    log_path = corrections_path(result.outputs.jsonl)
    assert f'corrections: "{log_path.name}"' in markdown.split("---")[1]
    assert "] こんとそのチームとAzureを使います" in markdown
    validate_artifact(result.outputs.manifest, verify_source=True)
    manifest = read_json(result.outputs.manifest)
    assert manifest["correction"]["run_id"] == corrected["run_id"]
    assert manifest["outputs"]["corrections"]["path"] == log_path.name
    log = read_json(log_path)
    run = log["runs"][0]
    assert run["status"] == "applied" and run["genai_profile"] == "codex-default"
    assert run["base_jsonl_sha256"] == manifest["outputs"]["jsonl"]["sha256"]
    assert run["context_files"][0]["path"] == str(glossary.resolve())
    assert [change["offsets"] for change in run["changes"]] == [[0], [9]]
    assert run["changes"][0]["start"] == 0.1
    assert sorted(item["reason"] for item in run["rejected"]) == sorted([
        "not_found", "unknown_segment", "overlap", "same_or_empty", "multiline", "too_long",
        "invalid",
    ])
    assert all(len(str(item["before"])) <= 101 for item in run["rejected"])
    # The same transcription settings keep the corrected files.
    assert _transcribe(tmp_path).status == "unchanged"
    assert "Contoso" in result.outputs.text.read_text(encoding="utf-8")


def test_revert_restores_the_original_files_byte_for_byte(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)
    hashes = {
        label: record["sha256"]
        for label, record in read_json(result.outputs.manifest)["outputs"].items()
    }
    bridge["responses"].append({"changes": [CONTOSO, AZURE]})
    correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    reverted = revert_corrections(result.outputs.jsonl)

    assert reverted["status"] == "reverted" and reverted["change_count"] == 2
    assert _rendered(result) == original
    manifest = read_json(result.outputs.manifest)
    assert manifest["correction"] is None
    assert all(manifest["outputs"][label]["sha256"] == value for label, value in hashes.items())
    assert read_json(corrections_path(result.outputs.jsonl))["runs"][0]["status"] == "reverted"
    validate_artifact(result.outputs.manifest, verify_source=True)
    assert revert_corrections(result.outputs.jsonl)["status"] == "unchanged"


def test_a_corrected_transcript_needs_redo_which_starts_from_the_original(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    config = make_config(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})
    first = correct_transcript(result.outputs.jsonl, config=config)

    with pytest.raises(ValidationError, match="already corrected"):
        correct_transcript(result.outputs.jsonl, config=config)
    skipped = correct_transcript(result.outputs.jsonl, config=config, if_needed=True)
    assert skipped["status"] == "unchanged" and skipped["run_id"] == first["run_id"]
    assert len(bridge["requests"]) == 1

    bridge["responses"].append({"changes": [AZURE]})
    second = correct_transcript(result.outputs.jsonl, config=config, redo=True)

    assert "こんとそのアジュールで会議します" in result.outputs.text.read_text(encoding="utf-8")
    assert "Azure" in result.outputs.text.read_text(encoding="utf-8")
    runs = read_json(corrections_path(result.outputs.jsonl))["runs"]
    assert [run["status"] for run in runs] == ["superseded", "applied"]
    assert runs[0]["superseded_by"] == second["run_id"]
    # Offsets of a redo still point into the original JSONL text.
    assert runs[1]["changes"][0]["offsets"] == [9]


def test_edited_files_are_neither_corrected_nor_reverted(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})
    correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    result.outputs.text.write_text("hand edited\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="changed after they were written"):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path), redo=True)
    with pytest.raises(ValidationError, match="changed after they were written"):
        revert_corrections(result.outputs.jsonl)
    assert result.outputs.text.read_text(encoding="utf-8") == "hand edited\n"
    assert len(bridge["requests"]) == 1


def test_dry_run_plans_every_request_without_network_or_writes(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("dry run must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(corrections, "MAX_BATCH_CHARACTERS", 30)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    planned = correct_transcript(result.outputs.jsonl, config=make_config(tmp_path), dry_run=True)

    assert planned["status"] == "planned" and planned["network_calls"] == 0
    assert planned["request_count"] == 2 and len(planned["plans"]) == 2
    assert planned["plans"][0]["will_call_provider"] is False
    assert bridge["requests"] == []
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_long_transcripts_are_corrected_in_several_requests(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    monkeypatch.setattr(corrections, "MAX_BATCH_CHARACTERS", 30)
    # The first request only sees segment 1, so its change to segment 2 is not used.
    bridge["responses"].extend([{"changes": [CONTOSO, AZURE]}, {"changes": [AZURE]}])

    corrected = correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert corrected["request_count"] == 2 and corrected["change_count"] == 2
    assert len(bridge["requests"]) == 2
    assert "こんとそのチーム" not in bridge["requests"][0].prompt
    run = read_json(corrections_path(result.outputs.jsonl))["runs"][0]
    assert [item["reason"] for item in run["rejected"]] == ["unknown_segment"]
    assert len(run["generation"]) == 2


def test_a_generation_failure_changes_nothing_and_hides_the_response(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)

    def fail(*args: Any) -> Any:
        raise GenAIError("private response content", code="provider_failed")

    monkeypatch.setattr(Runtime, "generate", fail)
    with pytest.raises(AudioTranscriberError, match="provider_failed") as caught:
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert "private" not in str(caught.value)
    assert "No transcript files were changed" in str(caught.value)
    assert _rendered(result) == original
    assert not corrections_path(result.outputs.jsonl).exists()


def test_writing_a_transcript_again_discards_the_applied_run_but_keeps_the_log(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})
    correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    result.outputs.markdown.write_text("edited\n", encoding="utf-8")

    rewritten = _transcribe(tmp_path, overwrite=True)

    assert rewritten.status == "replaced"
    assert "こんとそ" in rewritten.outputs.text.read_text(encoding="utf-8")
    manifest = read_json(rewritten.outputs.manifest)
    assert "correction" not in manifest
    assert manifest["outputs"]["corrections"]["path"] == corrections_path(result.outputs.jsonl).name
    validate_artifact(rewritten.outputs.manifest, verify_source=True)
    run = read_json(corrections_path(result.outputs.jsonl))["runs"][0]
    assert run["status"] == "discarded"
    bridge["responses"].append({"changes": [AZURE]})
    again = correct_transcript(rewritten.outputs.jsonl, config=make_config(tmp_path))
    assert again["status"] == "corrected"


def test_context_files_are_limited_and_must_be_utf8(tmp_path: Path, bridge: dict[str, Any]) -> None:
    result = _transcribe(tmp_path)
    large = tmp_path / "large.txt"
    large.write_text("x" * (corrections.MAX_CONTEXT_CHARACTERS + 1), encoding="utf-8")
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"\xff\xfe\x00broken")
    for context, message in ((large, "exceeds"), (binary, "not UTF-8")):
        with pytest.raises(ValidationError, match=message):
            correct_transcript(
                result.outputs.jsonl, config=make_config(tmp_path), context_files=[context]
            )
    assert bridge["requests"] == []


def _cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


def test_cli_correct_revert_and_dry_run(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _transcribe(tmp_path)
    _cli_environment(tmp_path, monkeypatch)
    jsonl = str(result.outputs.jsonl)
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("Contoso\n", encoding="utf-8")

    assert main(["correct", jsonl, "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    bridge["responses"].append({"changes": [CONTOSO]})
    assert main(["correct", jsonl, "--context-file", str(glossary), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "corrected"
    assert payload["changes"] == [
        {"segment_index": 1, "before": "こんとそ", "after": "Contoso", "kind": "proper_noun"}
    ]
    assert "--revert" in payload["review_hint"]
    assert main(["correct", jsonl, "--json"]) == 2
    assert "already corrected" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert main(["correct", jsonl, "--revert", "--dry-run", "--json"]) == 2
    capsys.readouterr()
    assert main(["correct", jsonl, "--revert", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reverted"
    assert main(["correct", jsonl, "--genai-profile", "missing", "--json"]) == 2
    assert "not defined in genai_profiles" in json.loads(capsys.readouterr().out)["error"][
        "message"
    ]


def test_cli_transcribe_correct_checks_the_connection_first_then_corrects(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_environment(tmp_path, monkeypatch)
    calls: list[str] = []

    def pipeline(*, config: ResolvedConfig, logger: Any) -> TranscriptionPipeline:
        calls.append("pipeline")
        return _pipeline(config)

    monkeypatch.setattr("audio_transcriber.cli.TranscriptionPipeline", pipeline)
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"meeting audio")
    base = ["transcribe", str(source), "--output-dir", str(tmp_path / "out"), "--correct"]
    make_config(tmp_path)  # Creates the local model files the pipeline checks.
    model_options = [
        "--model-dir", str(tmp_path / "models"), "--cache-dir", str(tmp_path / "cache"),
        "--state-dir", str(tmp_path / "state"), "--chunk-seconds", "10",
    ]

    assert main([*base, *model_options, "--genai-profile", "missing", "--json"]) == 2
    assert calls == []
    capsys.readouterr()
    assert main([*base, *model_options, "--dry-run", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "planned" and planned["correction"]["status"] == "planned"
    bridge["responses"].append({"changes": [CONTOSO]})
    assert main([*base, *model_options, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created"
    assert payload["correction"]["status"] == "corrected"
    assert payload["correction"]["change_count"] == 1
    assert main([*base, *model_options, "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["status"] == "unchanged" and again["correction"]["status"] == "unchanged"
    assert len(bridge["requests"]) == 1


CORRECTION = (
    "    profile: local/local-small\n"
    "    correction:\n"
    "      context_files: [glossary.txt]\n"
)


def _pipeline_factory(config: ResolvedConfig, logger: Any) -> TranscriptionPipeline:
    return _pipeline(config)


def test_pull_corrects_new_transcripts_of_sources_with_correction(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    _recording(tmp_path)
    (tmp_path / "glossary.txt").write_text("Contoso\n", encoding="utf-8")
    config_file = _config_file(tmp_path, CORRECTION, schema="5.0.0")
    bridge["responses"].append({"changes": [CONTOSO]})

    payload = _pull(tmp_path, config_file, pipeline_factory=_pipeline_factory)

    assert payload["status"] == "completed" and payload["correction_failures_count"] == 0
    item = payload["items"][0]
    assert item["correction"]["status"] == "corrected"
    assert item["correction"]["change_count"] == 1
    assert "glossary.txt" in bridge["requests"][0].prompt
    text = Path(item["outputs"]["text"]).read_text(encoding="utf-8")
    assert text.startswith("Contosoのアジュール")

    planned = _pull(tmp_path, config_file, pipeline_factory=_pipeline_factory, dry_run=True)
    assert planned["items"][0]["correction"] == {"status": "planned"}


def test_pull_reports_a_failed_correction_and_keeps_the_transcript(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _recording(tmp_path)
    (tmp_path / "glossary.txt").write_text("Contoso\n", encoding="utf-8")

    def fail(*args: Any) -> Any:
        raise GenAIError("private", code="provider_failed")

    monkeypatch.setattr(Runtime, "generate", fail)
    payload = _pull(
        tmp_path, _config_file(tmp_path, CORRECTION, schema="5.0.0"),
        pipeline_factory=_pipeline_factory,
    )

    assert payload["status"] == "partial" and payload["correction_failures_count"] == 1
    item = payload["items"][0]
    assert item["status"] == "transcribed" and item["correction"]["status"] == "failed"
    assert Path(item["outputs"]["jsonl"]).is_file()


def test_pull_checks_the_connection_before_copying_any_file(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    source = _recording(tmp_path)
    config_file = _config_file(
        tmp_path, CORRECTION.replace("glossary.txt", "missing.txt"), schema="5.0.0"
    )
    with pytest.raises(ValidationError, match="Cannot read context file"):
        _pull(tmp_path, config_file, pipeline_factory=_pipeline_factory)
    assert source.is_file() and not (tmp_path / "out").exists()


def test_source_correction_settings_resolve_from_the_config_file(tmp_path: Path) -> None:
    folder = tmp_path / "settings"
    folder.mkdir()
    path = folder / "config.yaml"
    path.write_text(
        'schema_version: "5.0.0"\nsources:\n'
        "  a:\n    input_dir: in\n    output_dir: out\n"
        "    correction:\n      context_files: [./vocabulary/a.txt]\n"
        "  b:\n    input_dir: in\n    output_dir: out\n    correction: {}\n"
        "  c:\n    input_dir: in\n    output_dir: out\n",
        encoding="utf-8",
    )
    sources = {
        source.name: source
        for source in resolve_config(
            cwd=tmp_path, home=tmp_path / "home", explicit_config=path
        ).input_sources
    }
    assert sources["a"].correction is not None
    assert sources["a"].correction.context_files == ((folder / "vocabulary" / "a.txt").resolve(),)
    assert sources["b"].correction is not None and sources["b"].correction.context_files == ()
    assert sources["c"].correction is None
    path.write_text(
        'schema_version: "5.0.0"\nsources:\n'
        "  a:\n    input_dir: in\n    output_dir: out\n    correction:\n      context_files: x\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="context_files must be a list"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)


def test_a_failed_replacement_puts_the_earlier_files_back(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)
    manifest = result.outputs.manifest.read_bytes()
    real_replace = os.replace

    def replace_except_srt(source: Any, target: Any) -> None:
        if Path(target) == result.outputs.srt:
            raise PermissionError("another program has the SRT open")
        real_replace(source, target)

    monkeypatch.setattr("audio_transcriber.corrections.os.replace", replace_except_srt)
    bridge["responses"].append({"changes": [CONTOSO, AZURE]})
    with pytest.raises(PermissionError):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert _rendered(result) == original
    assert result.outputs.manifest.read_bytes() == manifest
    assert not corrections_path(result.outputs.jsonl).exists()
    assert not list(result.outputs.jsonl.parent.glob(".*.pending"))
    validate_artifact(result.outputs.manifest, verify_source=True)


def test_files_edited_while_the_model_answers_are_kept(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    manifest = result.outputs.manifest.read_bytes()
    real_generate = Runtime.generate

    def edit_then_answer(self: Runtime, request: Any) -> Any:
        result.outputs.text.write_text("edited during generation\n", encoding="utf-8")
        return real_generate(self, request)

    monkeypatch.setattr(Runtime, "generate", edit_then_answer)
    bridge["responses"].append({"changes": [CONTOSO]})
    with pytest.raises(ValidationError, match="changed while the correction"):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert result.outputs.text.read_text(encoding="utf-8") == "edited during generation\n"
    assert result.outputs.manifest.read_bytes() == manifest
    assert not corrections_path(result.outputs.jsonl).exists()


def test_an_unreadable_log_stops_writing_the_transcript_again(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})
    correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    result.outputs.markdown.write_text("edited\n", encoding="utf-8")
    corrections_path(result.outputs.jsonl).write_text("{broken", encoding="utf-8")

    with pytest.raises(ValidationError, match="Cannot read corrections log"):
        _transcribe(tmp_path, overwrite=True)

    assert result.outputs.markdown.read_text(encoding="utf-8") == "edited\n"
    assert "Contoso" in result.outputs.text.read_text(encoding="utf-8")


def test_a_missing_provider_cli_is_found_before_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(tmp_path, monkeypatch)
    user = tmp_path / "home" / ".tkn" / "audio_transcriber" / "config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(
        'schema_version: "5.0.0"\ngenai_profiles:\n  codex-default:\n'
        f"    cli:\n      executable: '{tmp_path / 'missing' / 'codex.exe'}'\n",
        encoding="utf-8",
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "audio_transcriber.cli.TranscriptionPipeline",
        lambda **kwargs: calls.append("pipeline"),
    )
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"meeting audio")

    assert main(["transcribe", str(source), "--correct", "--json"]) == 2
    assert "codex CLI for genai_profiles.codex-default was not found" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]
    assert calls == []


def test_a_long_context_shrinks_the_requests_instead_of_failing(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    monkeypatch.setattr(corrections, "MAX_CONTEXT_CHARACTERS", 300_000)
    glossary = tmp_path / "glossary.txt"
    # Line breaks double in JSON, so this context takes most of a request.
    glossary.write_text("語\n" * 37_000, encoding="utf-8", newline="\n")
    corrected = correct_transcript(
        result.outputs.jsonl, config=make_config(tmp_path), context_files=[glossary]
    )
    assert corrected["status"] == "corrected"
    assert all(
        len(request.prompt) <= corrections.MAX_PROMPT_CHARACTERS
        for request in bridge["requests"]
    )
    glossary.write_text("語\n" * 60_000, encoding="utf-8")
    with pytest.raises(ValidationError, match="too long to leave room"):
        correct_transcript(
            result.outputs.jsonl, config=make_config(tmp_path), context_files=[glossary],
            redo=True,
        )


def test_line_separators_inside_text_stay_in_one_segment(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(TEXTS, "chunk_000000.wav", "こんとそ のアジュール")
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})

    corrected = correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert corrected["segment_count"] == 2 and corrected["change_count"] == 1
    assert '"text": "Contoso のアジュール"' not in result.outputs.jsonl.read_text(
        encoding="utf-8"
    )
    assert "Contoso のアジュール" in result.outputs.text.read_text(encoding="utf-8")


def test_a_changed_jsonl_during_generation_writes_nothing(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)
    real_generate = Runtime.generate

    def edit_then_answer(self: Runtime, request: Any) -> Any:
        edited = original["jsonl"].replace("会議".encode(), "相談".encode())
        result.outputs.jsonl.write_bytes(edited)
        return real_generate(self, request)

    monkeypatch.setattr(Runtime, "generate", edit_then_answer)
    bridge["responses"].append({"changes": [CONTOSO]})
    with pytest.raises(ValidationError, match="changed while the correction"):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    assert result.outputs.text.read_bytes() == original["text"]
    assert not corrections_path(result.outputs.jsonl).exists()


def test_only_the_transcripts_own_files_are_rewritten(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    other = tmp_path / "notes.txt"
    other.write_bytes(result.outputs.text.read_bytes())
    manifest = read_json(result.outputs.manifest)
    manifest["outputs"]["text"]["path"] = str(other)
    result.outputs.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValidationError, match="not this transcript's own files"):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    assert bridge["requests"] == []


def test_a_correction_in_progress_blocks_another_one(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)
    stem = result.outputs.jsonl.name.removesuffix(".jsonl")
    lock = result.outputs.jsonl.with_name(f".{stem}.corrections.lock")
    lock.write_text("", encoding="utf-8")
    bridge["responses"].append({"changes": [CONTOSO]})

    with pytest.raises(ValidationError, match="Another run is writing"):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    assert _rendered(result) == original and lock.exists()
    lock.unlink()
    bridge["responses"].append({"changes": [CONTOSO]})
    assert correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))["status"] == (
        "corrected"
    )
    assert not lock.exists()


def test_a_held_log_stops_writing_the_transcript_again_before_any_output_moves(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})
    correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    result.outputs.markdown.write_text("edited\n", encoding="utf-8")
    log = corrections_path(result.outputs.jsonl)
    real_replace = os.replace

    def replace_except_log(source: Any, target: Any) -> None:
        if Path(target) == log:
            raise PermissionError("another program has the log open")
        real_replace(source, target)

    monkeypatch.setattr("audio_transcriber.pipeline.os.replace", replace_except_log)
    with pytest.raises(PermissionError):
        _transcribe(tmp_path, overwrite=True)
    assert result.outputs.markdown.read_text(encoding="utf-8") == "edited\n"
    assert "Contoso" in result.outputs.text.read_text(encoding="utf-8")
    assert read_json(log)["runs"][0]["status"] == "applied"


def test_dry_run_of_transcribe_correct_checks_the_context_fits(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(corrections, "MAX_CONTEXT_CHARACTERS", 300_000)
    glossary = tmp_path / "glossary.txt"
    # Within the character limit, but each tab takes two characters in JSON.
    glossary.write_text("a\t" * 50_000, encoding="utf-8")
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"meeting audio")

    assert main([
        "transcribe", str(source), "--correct", "--context-file", str(glossary),
        "--dry-run", "--json",
    ]) == 2
    assert "too long to leave room" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_replacements_skip_matches_inside_longer_english_words(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(TEXTS, "chunk_000000.wav", "We use Net and Netflix with Net.")
    monkeypatch.setitem(TEXTS, "chunk_000001.wav", "Netflixだけを見ます")
    result = _transcribe(tmp_path)
    net = {"before": "Net", "after": "NET", "kind": "term", "reason": "製品名"}
    bridge["responses"].append({"changes": [
        {**net, "segment_index": 1}, {**net, "segment_index": 2},
    ]})

    corrected = correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert corrected["change_count"] == 1
    assert result.outputs.text.read_text(encoding="utf-8").splitlines() == [
        "We use NET and Netflix with NET.", "Netflixだけを見ます",
    ]
    run = read_json(corrections_path(result.outputs.jsonl))["runs"][0]
    assert run["changes"][0]["offsets"] == [7, 28]
    assert [item["reason"] for item in run["rejected"]] == ["inside_word"]


def test_replacements_that_cannot_be_saved_as_utf8_are_rejected(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [
        CONTOSO, {**AZURE, "after": "Azure\ud83d"}, {**AZURE, "before": "\ud83dx"},
    ]})

    corrected = correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))

    assert corrected["change_count"] == 1 and corrected["rejected_count"] == 2
    log = corrections_path(result.outputs.jsonl).read_bytes().decode("utf-8")
    assert [item["reason"] for item in json.loads(log)["runs"][0]["rejected"]] == [
        "invalid_text", "invalid_text",
    ]


def test_corrections_start_from_bytes_that_match_the_manifest(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    real_snapshot = corrections._snapshot

    def edited_after_validation(transcript_files: Any) -> dict[Path, bytes | None]:
        snapshot = real_snapshot(transcript_files)
        snapshot[transcript_files.text] = b"edited between checks\n"
        return snapshot

    monkeypatch.setattr(corrections, "_snapshot", edited_after_validation)
    with pytest.raises(ValidationError, match="Output hash does not match manifest"):
        correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    assert bridge["requests"] == []


def test_writing_a_transcript_again_waits_for_no_other_publisher(
    tmp_path: Path, bridge: dict[str, Any]
) -> None:
    result = _transcribe(tmp_path)
    original = _rendered(result)
    result.outputs.markdown.write_text("edited\n", encoding="utf-8")
    stem = result.outputs.jsonl.name.removesuffix(".jsonl")
    lock = result.outputs.jsonl.with_name(f".{stem}.corrections.lock")
    lock.write_text("", encoding="utf-8")

    with pytest.raises(ValidationError, match="Another run is writing"):
        _transcribe(tmp_path, overwrite=True)
    assert result.outputs.text.read_bytes() == original["text"]
    assert result.outputs.markdown.read_text(encoding="utf-8") == "edited\n"
    lock.unlink()
    assert _transcribe(tmp_path, overwrite=True).status == "replaced"
    assert not lock.exists()


def test_a_failed_output_replacement_puts_the_log_back(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _transcribe(tmp_path)
    bridge["responses"].append({"changes": [CONTOSO]})
    correct_transcript(result.outputs.jsonl, config=make_config(tmp_path))
    log = corrections_path(result.outputs.jsonl)
    before = log.read_bytes()
    result.outputs.text.write_text("edited\n", encoding="utf-8")
    real_replace = os.replace

    def replace_except_markdown(source: Any, target: Any) -> None:
        if Path(target) == result.outputs.markdown:
            raise PermissionError("another program has the Markdown open")
        real_replace(source, target)

    monkeypatch.setattr("audio_transcriber.pipeline.os.replace", replace_except_markdown)
    with pytest.raises(PermissionError):
        _transcribe(tmp_path, overwrite=True)
    assert log.read_bytes() == before
    assert not list(result.outputs.jsonl.parent.glob(".*.pending"))


def test_a_failed_correction_after_transcription_says_how_to_retry(
    tmp_path: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "audio_transcriber.cli.TranscriptionPipeline",
        lambda *, config, logger: _pipeline(config),
    )

    def fail(*args: Any) -> Any:
        raise GenAIError("private", code="provider_failed")

    monkeypatch.setattr(Runtime, "generate", fail)
    make_config(tmp_path)
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"meeting audio")
    assert main([
        "transcribe", str(source), "--correct", "--output-dir", str(tmp_path / "out"),
        "--model-dir", str(tmp_path / "models"), "--cache-dir", str(tmp_path / "cache"),
        "--state-dir", str(tmp_path / "state"), "--chunk-seconds", "10", "--json",
    ]) == 2
    message = json.loads(capsys.readouterr().out)["error"]["message"]
    assert "same transcribe --correct command" in message and "private" not in message

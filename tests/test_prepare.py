from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_pipeline import FakeAzureRecognizer, FakeFfmpeg, make_logger
from tkn_genai_bridge import Profile, ProviderError, Runtime

from audio_transcriber.cli import main
from audio_transcriber.config import resolve_config
from audio_transcriber.errors import (
    AudioTranscriberError,
    CloudUploadApprovalError,
    ValidationError,
)
from audio_transcriber.pipeline import TranscriptionPipeline
from audio_transcriber.prepare import prepare_transcription


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source audio")
    state: dict[str, Any] = {
        "audio": source,
        "ffmpeg": [],
        "azure": [],
        "generation": [],
        "fail_generation": False,
    }
    state["config"] = resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        profile="cloud/azure-ja",
        cli_overrides={
            "output_dir": str(tmp_path / "run"),
            "state_dir": str(tmp_path / "state"),
            "azure_speech_endpoint": "https://example.cognitiveservices.azure.com/",
        },
    )

    def pipeline(*, config: Any, logger: Any) -> TranscriptionPipeline:
        return TranscriptionPipeline(
            config=config,
            logger=logger,
            ffmpeg=FakeFfmpeg(state["ffmpeg"]),
            azure_recognizer_factory=lambda cfg, log: FakeAzureRecognizer(state["azure"]),
        )

    def generate(self: Any, request: Any) -> Any:
        state["generation"].append(request)
        if state["fail_generation"]:
            raise ProviderError("failed", code="provider_failed")
        return SimpleNamespace(
            data={
                "candidates": [
                    {
                        "term": "Azure",
                        "kind": "extracted",
                        "reason": "Service name",
                        "evidence": [{"segment_index": 1, "quote": "Azure text"}],
                    }
                ]
            },
            record=SimpleNamespace(model_dump=lambda **kwargs: {"status": "succeeded"}),
        )

    monkeypatch.setattr("audio_transcriber.prepare.TranscriptionPipeline", pipeline)
    monkeypatch.setattr("tkn_genai_bridge.load_profile", lambda name: Profile(provider="codex"))
    monkeypatch.setattr(Runtime, "generate", generate)
    return state


def run(workflow: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return prepare_transcription(
        workflow["audio"],
        config=workflow["config"],
        logger=make_logger(),
        **kwargs,
    )


def test_prepare_produces_transcript_and_review_then_stops(
    tmp_path: Path, workflow: dict[str, Any]
) -> None:
    result = run(workflow, allow_cloud_upload=True)
    assert result["status"] == "review_required"
    assert result["stop_after"] == "phrase_review"
    assert result["transcription"]["status"] == "created"
    media = Path(result["transcription"]["outputs"]["audio"]).parent
    assert media.parent == tmp_path / "run"
    assert len(list(media.iterdir())) == 6
    assert (media / "audio.flac").is_file()
    assert len(list((media / "vocabulary").iterdir())) == 4
    assert not (tmp_path / "run/transcript").exists()
    assert not (tmp_path / "run/vocabulary").exists()
    assert len(workflow["azure"]) == len(workflow["generation"]) == 1
    assert "Azure text" in workflow["generation"][0].prompt


def test_generation_failure_resumes_without_another_audio_submission(
    tmp_path: Path, workflow: dict[str, Any]
) -> None:
    workflow["fail_generation"] = True
    with pytest.raises(AudioTranscriberError, match="rerun the same transcribe --suggest-phrases"):
        run(workflow, allow_cloud_upload=True)
    assert list((tmp_path / "run").glob("*/transcript_*.jsonl"))
    assert not list((tmp_path / "run").glob("*/vocabulary"))
    workflow["fail_generation"] = False
    result = run(workflow, allow_cloud_upload=True)
    assert result["transcription"]["status"] == "unchanged"
    assert len(workflow["azure"]) == 1
    assert len(workflow["generation"]) == 2
    terms = Path(result["transcription"]["outputs"]["jsonl"]).parent / "vocabulary/phrases.txt"
    terms.write_text("Human correction\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="already exists"):
        run(workflow, allow_cloud_upload=True)
    assert terms.read_text() == "Human correction\n"
    assert len(workflow["azure"]) == 1
    assert len(workflow["generation"]) == 2


def test_prepare_dry_run_never_transcribes_generates_or_writes(
    tmp_path: Path, workflow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("network accessed in dry run")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    before = list(tmp_path.rglob("*"))
    result = run(workflow, dry_run=True)
    assert result["status"] == "planned" and result["network_calls"] == 0
    assert result["suggestions"]["token_estimate"] is None
    assert result["suggestions"]["transcript_validation"] == "after_transcription"
    assert result["transcription"]["plan"]["network_calls"] == 0
    assert list(tmp_path.rglob("*")) == before
    assert not workflow["ffmpeg"] and not workflow["azure"] and not workflow["generation"]


@pytest.mark.parametrize("problem", ["context", "limit", "profile", "occupied"])
def test_invalid_generation_inputs_fail_before_transcription(
    tmp_path: Path, workflow: dict[str, Any], problem: str
) -> None:
    kwargs: dict[str, Any] = {}
    if problem == "context":
        kwargs["context_file"] = tmp_path / "missing.txt"
    elif problem == "limit":
        kwargs["max_candidates"] = 0
    elif problem == "profile":
        kwargs["generation_profile"] = "missing"
    else:
        plan = run(workflow, dry_run=True)
        Path(plan["suggestions"]["output_dir"]).mkdir(parents=True)
    with pytest.raises(AudioTranscriberError):
        run(workflow, allow_cloud_upload=True, **kwargs)
    assert not workflow["ffmpeg"] and not workflow["azure"] and not workflow["generation"]


def test_cloud_upload_permission_still_required(workflow: dict[str, Any]) -> None:
    with pytest.raises(CloudUploadApprovalError):
        run(workflow)
    assert not workflow["azure"] and not workflow["generation"]


@pytest.mark.parametrize("output_mode", ["cli", "config", "default"])
def test_prepare_cli_json_and_options(
    tmp_path: Path,
    workflow: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_mode: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    args = [
        "transcribe", "--suggest-phrases",
        str(workflow["audio"]),
        "--profile",
        "cloud/azure-ja",
        "--output-dir",
        str(tmp_path / "run"),
        "--state-dir",
        str(tmp_path / "state"),
        "--azure-speech-endpoint",
        "https://example.cognitiveservices.azure.com/",
        "--allow-cloud-upload",
        "--generation-profile",
        "codex",
        "--max-candidates",
        "10",
        "--json",
    ]
    expected_root = tmp_path / "run"
    if output_mode != "cli":
        index = args.index("--output-dir")
        del args[index:index + 2]
        if output_mode == "config":
            config_path = tmp_path / "settings.yaml"
            config_path.write_text(json.dumps({
                "schema_version": "3.1.0",
                "folders": {"output": str(expected_root)},
            }), encoding="utf-8")
            args.extend(["--config", str(config_path)])
        else:
            expected_root = tmp_path
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    media = Path(result["transcription"]["outputs"]["jsonl"]).parent
    assert media.parent == expected_root
    assert (media / "vocabulary/phrases.txt").is_file()
    assert list(media.glob("transcript_*.jsonl"))
    assert result["status"] == "review_required"
    assert result["suggestions"]["max_candidates"] == 10
    assert result["suggestions"]["generation_profile"] == "codex"
    assert main(args) == 2
    assert capsys.readouterr().out == ""
    assert len(workflow["azure"]) == len(workflow["generation"]) == 1


def test_prepare_rejects_overwrite() -> None:
    for args in (
        ["transcribe", "--suggest-phrases", "audio.wav", "--output-dir", "run", "--overwrite"],
    ):
        with pytest.raises(SystemExit) as caught:
            main(args)
        assert caught.value.code == 2


def test_suggestions_share_normal_transcription_paths(workflow: dict[str, Any]) -> None:
    normal = TranscriptionPipeline(
        config=workflow["config"], logger=make_logger()
    ).transcribe(workflow["audio"], dry_run=True, overwrite=False)
    planned = run(workflow, dry_run=True)
    assert planned["transcription"]["outputs"] == normal.outputs.as_dict()
    assert Path(planned["suggestions"]["output_dir"]) == normal.outputs.jsonl.parent / "vocabulary"
    actual = run(workflow, allow_cloud_upload=True)
    assert actual["transcription"]["outputs"] == normal.outputs.as_dict()
    assert Path(planned["suggestions"]["output_dir"]).is_dir()


def test_different_audio_has_its_own_vocabulary(
    tmp_path: Path, workflow: dict[str, Any]
) -> None:
    first = run(workflow, allow_cloud_upload=True)
    first_terms = Path(first["transcription"]["outputs"]["jsonl"]).parent / "vocabulary/phrases.txt"
    first_terms.write_text("Reviewed term", encoding="utf-8")
    second_audio = tmp_path / "second.wav"
    second_audio.write_bytes(b"second source audio")
    workflow["audio"] = second_audio
    second = run(workflow, allow_cloud_upload=True)
    second_media = Path(second["transcription"]["outputs"]["jsonl"]).parent
    assert (second_media / "vocabulary/phrases.txt").is_file()
    assert second_media != first_terms.parent.parent
    assert first_terms.read_text(encoding="utf-8") == "Reviewed term"
    assert len(workflow["azure"]) == len(workflow["generation"]) == 2

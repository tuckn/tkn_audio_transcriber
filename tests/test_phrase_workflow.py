from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from tkn_genai_bridge import GenAIError, Profile, Runtime

from audio_transcriber.cli import main
from audio_transcriber.config import ConfigError, resolve_config
from audio_transcriber.errors import AudioTranscriberError, ValidationError
from audio_transcriber.phrase_files import read_phrase_files
from audio_transcriber.phrase_suggestions import suggest_phrases
from audio_transcriber.pipeline import _fingerprint


def write_config(path: Path, phrases: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.1.0",
                "transcription": {
                    "cloud": {
                        "profiles": {
                            "azure-ja": {
                                "endpoint": "https://example.cognitiveservices.azure.com/",
                                "phrase_list": phrases,
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_phrase_file_origins_merge_bom_comments_and_content_fingerprint(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_config = home / ".tkn/audio_transcriber/config.yaml"
    write_config(user_config, {"phrases": ["  Common  "], "files": ["common.txt"]})
    common = user_config.parent / "common.txt"
    common.write_bytes("\ufeff# 注記\r\nCommon\r\nC#\r\nMicrosoft Azure\r\n\r\n".encode())
    extra = tmp_path / "meeting.txt"
    extra.write_text("Microsoft Azure\n用語\n", encoding="utf-8")
    explicit = tmp_path / "settings/override.yaml"
    # Overriding inline words must not change the base directory of inherited files.
    write_config(explicit, {"phrases": ["Common", "Inline"]})

    def resolve() -> Any:
        return resolve_config(
            cwd=tmp_path,
            home=home,
            explicit_config=explicit,
            profile="cloud/azure-ja",
            phrase_files=[Path("meeting.txt")],
        )

    first = resolve()
    assert first.transcription.phrases == ("Common", "Inline", "C#", "Microsoft Azure", "用語")
    assert first.phrase_file_sources[0]["path"] == str(common)
    assert first.phrase_file_sources[1]["path"] == str(extra)
    assert "phrase files" in first.sources["azure_speech_phrases"]
    assert first.display()["phrase_files"] == list(first.phrase_file_sources)
    extra.write_text("修正後\n", encoding="utf-8")
    second = resolve()
    assert _fingerprint("audio", 10, first) != _fingerprint("audio", 10, second)
    assert first.phrase_file_sources[1]["sha256"] != second.phrase_file_sources[1]["sha256"]
    # A higher config layer's file list replaces the lower list; it resolves beside that layer.
    write_config(explicit, {"files": ["replacement.txt"]})
    (explicit.parent / "replacement.txt").write_text("Replacement", encoding="utf-8")
    third = resolve()
    assert third.transcription.phrases == ("Common", "Replacement", "修正後")


@pytest.mark.parametrize(
    "content", [b"\xff", b"word\x00bad", b"x" * 1_000_001],
    ids=["invalid-utf8", "control-character", "oversized"],
)
def test_bad_phrase_files_are_rejected(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "terms.txt"
    path.write_bytes(content)
    with pytest.raises(ConfigError):
        read_phrase_files([], [path])


def test_phrase_file_limits_missing_and_local_rejection(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read"):
        read_phrase_files([], [tmp_path / "missing.txt"])
    path = tmp_path / "terms.txt"
    path.write_text("\n".join(f"word{i}" for i in range(500)), encoding="utf-8")
    assert len(read_phrase_files(["word0"], [path, path])[0]) == 500
    with pytest.raises(ConfigError, match="500"):
        read_phrase_files(["extra"], [path])
    with pytest.raises(ConfigError, match="cloud"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", phrase_files=[path])


def test_file_phrases_require_supported_api(tmp_path: Path) -> None:
    path = tmp_path / "terms.txt"
    path.write_text("Example", encoding="utf-8")
    with pytest.raises(ConfigError, match="2025-10-15"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            profile="cloud/azure-ja",
            phrase_files=[path],
            cli_overrides={
                "azure_speech_endpoint": "https://example.cognitiveservices.azure.com",
                "azure_speech_api_version": "2024-11-15",
            },
        )


def test_commented_out_phrase_block_clears_inherited_words(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / ".tkn/audio_transcriber/config.yaml"
    write_config(user, {"phrases": ["Inherited"], "files": ["missing.txt"]})
    explicit = tmp_path / "override.yaml"
    explicit.write_text(
        'schema_version: "3.0.0"\ntranscription:\n  cloud:\n    profiles:\n'
        '      azure-ja:\n        phrase_list:\n          # phrases: []\n', encoding="utf-8"
    )
    before = explicit.read_bytes()
    config = resolve_config(cwd=tmp_path, home=home, explicit_config=explicit,
                            profile="cloud/azure-ja")
    assert config.transcription.phrases == ()
    assert config.phrase_file_sources == ()
    assert explicit.read_bytes() == before


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps(
            {
                "index": 1,
                "start": 61.5,
                "end": 65.0,
                "text": "Microsoft Azureで、こんとそを使います。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "selected": [],
        "data": {
            "candidates": [
                {
                    "term": "Microsoft Azure",
                    "kind": "extracted",
                    "reason": "製品名",
                    "evidence": [{"segment_index": 1, "quote": "Microsoft Azureで"}],
                },
                {
                    "term": "Contoso",
                    "kind": "inferred_correction",
                    "reason": "表記を音声で確認",
                    "evidence": [{"segment_index": 1, "quote": "こんとそを使います"}],
                },
            ]
        },
    }

    def load(name: str) -> Profile:
        state["selected"].append(name)
        return Profile(provider="codex")

    def generate(self: Runtime, request: Any) -> Any:
        state["calls"].append(request)
        return SimpleNamespace(
            data=state["data"],
            record=SimpleNamespace(
                model_dump=lambda **kwargs: {"provider": "codex", "status": "succeeded"}
            ),
        )

    monkeypatch.setattr("tkn_genai_bridge.load_profile", load)
    monkeypatch.setattr(Runtime, "generate", generate)
    return state


def test_suggest_review_edit_and_resolve_without_overwrite(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any]
) -> None:
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home")
    original = transcript.read_bytes()
    context = tmp_path / "context.txt"
    context.write_text("資料にある製品名はContosoです。", encoding="utf-8")
    target = tmp_path / "review"
    result = suggest_phrases(transcript, output_dir=target, config=config, context_file=context)
    assert result["status"] == "review_required"
    assert result["candidate_count"] == 2
    assert bridge["selected"] == ["codex-default"]
    assert "資料にある製品名" in bridge["calls"][0].prompt
    review = (target / "review.md").read_text(encoding="utf-8")
    assert "00:01:01–00:01:05" in review and "訂正の推測" in review
    assert "こんとそを使います" in review
    candidates = json.loads((target / "candidates.json").read_text(encoding="utf-8"))
    assert candidates["candidates"][0]["evidence"][0]["start"] == 61.5
    assert len(json.loads((target / "manifest.json").read_text())["outputs"]) == 3
    terms = target / "phrases.txt"
    terms.write_text("Microsoft Azure\nCorrected Company\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="already exists"):
        suggest_phrases(transcript, output_dir=target, config=config)
    assert len(bridge["calls"]) == 1
    assert "Corrected Company" in terms.read_text()
    assert transcript.read_bytes() == original
    cloud = resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        profile="cloud/azure-ja",
        phrase_files=[terms],
        cli_overrides={"azure_speech_endpoint": "https://example.cognitiveservices.azure.com"},
    )
    assert cloud.transcription.phrases == ("Microsoft Azure", "Corrected Company")


def test_dry_run_uses_real_bridge_plan_without_io(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("dry run must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home")
    before = sorted(tmp_path.rglob("*"))
    result = suggest_phrases(
        transcript, output_dir=tmp_path / "new/review", config=config, dry_run=True
    )
    assert result["network_calls"] == 0
    assert result["plan"]["will_call_provider"] is False
    assert result["plan"]["prompt_characters"] > 0
    assert bridge["calls"] == []
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("mutation", ["quote", "index", "kind", "multiline", "limit"])
def test_ungrounded_or_invalid_candidates_leave_no_output(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any], mutation: str
) -> None:
    item = bridge["data"]["candidates"][0]
    if mutation == "quote":
        item["evidence"][0]["quote"] = "not actually said"
    elif mutation == "index":
        item["evidence"][0]["segment_index"] = 2
    elif mutation == "kind":
        bridge["data"]["candidates"][1]["kind"] = "extracted"
    elif mutation == "multiline":
        item["term"] = "one\ntwo"
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home")
    with pytest.raises(ValidationError):
        suggest_phrases(
            transcript,
            output_dir=tmp_path / "review",
            config=config,
            max_candidates=1 if mutation == "limit" else 30,
        )
    assert not (tmp_path / "review").exists()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"text": "text"}',
        '{"index":true,"start":0,"end":2,"text":"text"}',
        '{"index":1,"start":0,"end":NaN,"text":"text"}',
    ],
)
def test_invalid_source_never_calls_provider(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any], text: str
) -> None:
    transcript.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        suggest_phrases(
            transcript,
            output_dir=tmp_path / "review",
            config=resolve_config(cwd=tmp_path, home=tmp_path / "home"),
        )
    assert not bridge["calls"]


def test_generation_profiles_limits_and_empty_result(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any]
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "3.1.0"\ngeneration:\n  active_profile: local\n'
        "  profiles:\n    local:\n      bridge_profile: local-text\n"
        "  max_candidates: 12\n",
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    assert "profiles.local.bridge_profile" in config.generation_sources
    bridge["data"] = {"candidates": []}
    result = suggest_phrases(transcript, output_dir=tmp_path / "review", config=config)
    assert result["candidate_count"] == 0 and result["max_candidates"] == 12
    assert bridge["selected"] == ["local-text"]
    assert read_phrase_files([], [tmp_path / "review/phrases.txt"])[0] == []
    with pytest.raises(ConfigError, match="Unknown"):
        suggest_phrases(
            transcript, output_dir=tmp_path / "other", config=config, generation_profile="missing"
        )


def test_bridge_failure_hides_private_response(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> Any:
        raise GenAIError("private response content", code="provider_failed")

    monkeypatch.setattr(Runtime, "generate", fail)
    with pytest.raises(AudioTranscriberError, match="provider_failed") as caught:
        suggest_phrases(
            transcript,
            output_dir=tmp_path / "review",
            config=resolve_config(cwd=tmp_path, home=tmp_path / "home"),
        )
    assert "private" not in str(caught.value)
    assert not (tmp_path / "review").exists()


def test_cli_suggest_json_and_dry_run(
    tmp_path: Path,
    transcript: Path,
    bridge: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        main(
            [
                "suggest-phrases",
                str(transcript),
                "--output-dir",
                str(tmp_path / "review"),
                "--generation-profile",
                "codex",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert bridge["calls"] == []
    assert (
        main(
            [
                "suggest-phrases",
                str(transcript),
                "--output-dir",
                str(tmp_path / "review"),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "review_required"

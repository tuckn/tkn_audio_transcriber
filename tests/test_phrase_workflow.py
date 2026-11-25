from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from tkn_genai_bridge import GenAIError, Profile, Runtime

from audio_transcriber.cli import main
from audio_transcriber.config import ConfigError, migrate_config_file, resolve_config
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
        "profiles": [],
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

    def shared_bridge_config(*args: Any, **kwargs: Any) -> Profile:
        pytest.fail("the shared tkn-genai-bridge config must not be read")

    def generate(self: Runtime, request: Any) -> Any:
        state["calls"].append(request)
        state["selected"].append(self.profile_name)
        state["profiles"].append(self.profile)
        return SimpleNamespace(
            data=state["data"],
            record=SimpleNamespace(
                model_dump=lambda **kwargs: {"provider": "codex", "status": "succeeded"}
            ),
        )

    monkeypatch.setattr("tkn_genai_bridge.load_profile", shared_bridge_config)
    monkeypatch.setattr("tkn_genai_bridge.load_config", shared_bridge_config)
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
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n"
        "  local-text:\n    provider: ollama\n    model: gemma4:latest\n    local_only: true\n"
        "generation:\n  active_profile: local\n"
        "  profiles:\n    local:\n      genai_profile: local-text\n"
        "  max_candidates: 12\n",
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    assert "profiles.local.genai_profile" in config.generation_sources
    assert config.genai_profile_sources["local-text.provider"].endswith(str(path))
    bridge["data"] = {"candidates": []}
    result = suggest_phrases(transcript, output_dir=tmp_path / "review", config=config)
    assert result["candidate_count"] == 0 and result["max_candidates"] == 12
    assert result["genai_profile"] == "local-text"
    assert bridge["selected"] == ["local-text"]
    assert bridge["profiles"][0].provider == "ollama"
    assert bridge["profiles"][0].local_only is True
    manifest = json.loads((tmp_path / "review/manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2.0.0"
    assert manifest["genai_profile"] == "local-text" and "bridge_profile" not in manifest
    assert read_phrase_files([], [tmp_path / "review/phrases.txt"])[0] == []
    with pytest.raises(ConfigError, match="Unknown"):
        suggest_phrases(
            transcript, output_dir=tmp_path / "other", config=config, generation_profile="missing"
        )


def test_genai_profiles_merge_by_field_and_resolve_executables_per_file(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any]
) -> None:
    home = tmp_path / "home"
    user = home / ".tkn" / "audio_transcriber" / "config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n"
        "  codex-default:\n    reasoning_effort: high\n"
        "    cli:\n      executable: tools/codex.exe\n"
        "  claude-default:\n    cli:\n      executable: claude\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "project" / "config.yaml"
    explicit.parent.mkdir()
    explicit.write_text(
        'schema_version: "4.0.0"\ngenai_profiles:\n  codex-default:\n    timeout_seconds: 42\n',
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=home, explicit_config=explicit)
    codex = config.genai_profiles["codex-default"]
    assert codex["provider"] == "codex" and codex["reasoning_effort"] == "high"
    assert codex["timeout_seconds"] == 42
    assert codex["cli"]["executable"] == str((user.parent / "tools" / "codex.exe").resolve())
    assert config.genai_profiles["claude-default"]["cli"]["executable"] == "claude"
    assert config.genai_profile_sources["codex-default.provider"] == "built-in default"
    assert config.genai_profile_sources["codex-default.timeout_seconds"].endswith(str(explicit))
    suggest_phrases(transcript, output_dir=tmp_path / "review", config=config)
    assert bridge["selected"] == ["codex-default"]
    assert bridge["profiles"][0].reasoning_effort == "high"
    assert bridge["profiles"][0].timeout_seconds == 42


def test_undefined_or_invalid_genai_profile_fails_before_generation(
    tmp_path: Path, transcript: Path, bridge: dict[str, Any]
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n"
        "  broken:\n    provider: codex\n    timeout_seconds: private-value\n"
        # Full-width digits pass the date pattern, and the date parser quotes the value.
        "  priced:\n    provider: codex\n    pricing:\n      m:\n        currency: JPY\n"
        "        pricing_date: '２０２６-０１-０１'\n"
        "        input_per_million: 1\n        output_per_million: 1\n"
        "generation:\n  profiles:\n"
        "    missing:\n      genai_profile: codex-high\n"
        "    broken:\n      genai_profile: broken\n"
        "    priced:\n      genai_profile: priced\n",
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    with pytest.raises(ConfigError, match="not defined in genai_profiles"):
        suggest_phrases(
            transcript, output_dir=tmp_path / "a", config=config, generation_profile="missing"
        )
    with pytest.raises(ConfigError, match=r"genai_profiles\.broken: timeout_seconds \(") as caught:
        suggest_phrases(
            transcript, output_dir=tmp_path / "b", config=config, generation_profile="broken"
        )
    assert "private-value" not in str(caught.value)
    with pytest.raises(ConfigError, match=r"pricing\.m\.pricing_date \(") as caught:
        suggest_phrases(
            transcript, output_dir=tmp_path / "c", config=config, generation_profile="priced"
        )
    assert "２０２６" not in str(caught.value)
    assert bridge["calls"] == []
    assert not any((tmp_path / name).exists() for name in ("a", "b", "c"))


def test_config_migrate_does_not_write_ai_connections_over_user_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / ".tkn" / "audio_transcriber" / "config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n  codex-default:\n    provider: claude-code\n"
        "generation:\n  active_profile: local\n"
        "  profiles:\n    local:\n      genai_profile: codex-default\n",
        encoding="utf-8",
    )
    project = tmp_path / "project.yaml"
    project.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")
    assert migrate_config_file(project)["status"] == "migrated"
    migrated = yaml.safe_load(project.read_text(encoding="utf-8"))
    assert "genai_profiles" not in migrated and "generation" not in migrated
    config = resolve_config(cwd=tmp_path, home=home, explicit_config=project)
    assert config.values["language"] == "en"
    assert config.genai_profiles["codex-default"]["provider"] == "claude-code"
    assert config.generation["active_profile"] == "local"


def test_config_migrate_keeps_only_generation_values_the_file_set(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / ".tkn" / "audio_transcriber" / "config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n  private:\n    provider: ollama\n    model: m\n    local_only: true\n"
        "generation:\n  profiles:\n    codex:\n      genai_profile: private\n",
        encoding="utf-8",
    )
    project = tmp_path / "project.yaml"
    project.write_text('schema_version: "2.0.0"\ngeneration:\n  max_candidates: 5\n')
    before = resolve_config(cwd=tmp_path, home=home, explicit_config=project)
    migrate_config_file(project)
    assert yaml.safe_load(project.read_text(encoding="utf-8"))["generation"] == {
        "max_candidates": 5
    }
    after = resolve_config(cwd=tmp_path, home=home, explicit_config=project)
    assert after.generation == before.generation
    assert after.generation["profiles"]["codex"]["genai_profile"] == "private"


def test_schema_2_bridge_profile_guides_through_config_migrate(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    legacy = (
        'schema_version: "2.0.0"\n'
        "transcription:\n  active_profile: local-small\n"
        "generation:\n  profiles:\n    codex:\n      bridge_profile: codex-default\n"
    )
    path.write_text(legacy, encoding="utf-8")
    with pytest.raises(ConfigError, match="config migrate"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    path.write_text(legacy.replace("bridge_profile", "genai_profile"), encoding="utf-8")
    assert migrate_config_file(path)["status"] == "migrated"
    migrated = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == "4.0.0"
    assert migrated["generation"] == {"profiles": {"codex": {"genai_profile": "codex-default"}}}
    assert migrated["transcription"]["active"] == {"mode": "local", "profile": "local-small"}


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ("    timeout_seconds: .nan\n", r"codex-default\.timeout_seconds must be a finite"),
        ("    pricing: !!set {a: null}\n", r"codex-default\.pricing has an unsupported YAML"),
        ("    pricing: &loop {m: *loop}\n", r"codex-default\.pricing\.m must not contain itself"),
        ('    model: "\\ud83d"\n', r"codex-default\.model has text that cannot be written"),
    ],
)
def test_genai_profiles_reject_values_config_list_cannot_print(
    tmp_path: Path, profile: str, message: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f'schema_version: "4.0.0"\ngenai_profiles:\n  codex-default:\n{profile}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)


@pytest.mark.parametrize(
    "body",
    [
        'genai_profiles:\n  "\\ud83d":\n    provider: codex\n',
        'generation:\n  profiles:\n    codex:\n      genai_profile: "\\ud83d"\n',
    ],
)
def test_genai_names_and_references_must_be_utf8(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f'schema_version: "4.0.0"\n{body}', encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot be written as UTF-8"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)


def test_unexpandable_executable_does_not_block_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.expanduser

    def expanduser(self: Path) -> Path:
        if str(self).startswith("~other"):
            raise RuntimeError("Could not determine home directory.")
        return original(self)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n  unused:\n    provider: codex\n"
        "    cli:\n      executable: ~other/tools/codex.exe\n",
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    assert config.genai_profiles["unused"]["cli"]["executable"] == "~other/tools/codex.exe"


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths exist only on Windows")
def test_drive_relative_executable_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n  codex-default:\n    cli:\n      executable: 'D:codex.exe'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be an absolute path"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)


def test_removed_genai_fields_drop_their_sources(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / ".tkn" / "audio_transcriber" / "config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(
        'schema_version: "4.0.0"\n'
        "genai_profiles:\n  codex-default:\n    cli:\n      executable: codex\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "config.yaml"
    explicit.write_text(
        'schema_version: "4.0.0"\ngenai_profiles:\n  codex-default:\n    cli: null\n',
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=home, explicit_config=explicit)
    assert config.genai_profiles["codex-default"]["cli"] is None
    assert "codex-default.cli.executable" not in config.genai_profile_sources
    assert config.genai_profile_sources["codex-default.cli"].endswith(str(explicit))
    # An empty mapping over null is the value the later file set.
    user.write_text(explicit.read_text(encoding="utf-8"), encoding="utf-8")
    explicit.write_text(
        'schema_version: "4.0.0"\ngenai_profiles:\n  codex-default:\n    cli: {}\n',
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=home, explicit_config=explicit)
    assert config.genai_profiles["codex-default"]["cli"] == {}
    assert config.genai_profile_sources["codex-default.cli"].endswith(str(explicit))
    # An empty mapping that a later file fills is no longer an effective value.
    user.write_text(
        'schema_version: "4.0.0"\ngeneration:\n  profiles:\n    custom: {}\n', encoding="utf-8"
    )
    explicit.write_text(
        'schema_version: "4.0.0"\n'
        "generation:\n  profiles:\n    custom:\n      genai_profile: codex-default\n",
        encoding="utf-8",
    )
    config = resolve_config(cwd=tmp_path, home=home, explicit_config=explicit)
    assert "profiles.custom" not in config.generation_sources
    assert config.generation_sources["profiles.custom.genai_profile"].endswith(str(explicit))


@pytest.mark.parametrize("version", ["3.3.0", "4.0.0"])
def test_bridge_profile_is_rejected_with_rename_guidance(tmp_path: Path, version: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f'schema_version: "{version}"\n'
        "generation:\n  profiles:\n    codex:\n      bridge_profile: codex-default\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as caught:
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    message = str(caught.value)
    assert "generation.profiles.codex.bridge_profile was renamed to genai_profile" in message
    assert "genai_profiles" in message and 'schema_version: "4.0.0"' in message


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("genai_profiles:\n  custom:\n    provider: codex\n", "genai_profiles"),
        (
            "generation:\n  profiles:\n    codex:\n      genai_profile: codex-default\n",
            "generation.profiles.codex.genai_profile",
        ),
    ],
)
def test_genai_keys_require_schema_4(tmp_path: Path, body: str, key: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f'schema_version: "3.3.0"\n{body}', encoding="utf-8")
    with pytest.raises(ConfigError, match=rf'{key} requires schema_version "4.0.0"'):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)


def test_schema_3_without_bridge_profile_is_read_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "3.3.0"\ngeneration:\n  max_candidates: 7\n', encoding="utf-8"
    )
    config = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=path)
    assert not config.has_in_memory_migrations
    assert config.config_sources[-1]["effective_schema_version"] == "4.0.0"
    assert config.generation["max_candidates"] == 7
    assert config.generation["profiles"] == {"codex": {"genai_profile": "codex-default"}}


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

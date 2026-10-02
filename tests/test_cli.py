import json
from pathlib import Path

import pytest

from audio_transcriber.cli import _config_lines, main
from audio_transcriber.errors import ValidationError
from audio_transcriber.models import OutputPaths, TranscriptionResult


def test_help_and_version(capsys: object) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "--profile" in captured.out


def test_transcribe_help_describes_audio_and_video(capsys: object) -> None:
    try:
        main(["transcribe", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "Source audio or video file with an audio stream" in captured.out
    assert "--provider" not in captured.out
    assert "cached credentials cannot be reused" in " ".join(captured.out.split())
    assert "--json" in captured.out


def test_transcribe_prints_copyable_paths_by_default(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    source = tmp_path / "source.wav"
    outputs = OutputPaths(
        markdown=tmp_path / "meeting_transcript.md",
        srt=tmp_path / "meeting_transcript.srt",
        jsonl=tmp_path / "meeting_transcript.jsonl",
        manifest=tmp_path / "meeting_transcript.manifest.json",
    )
    result = TranscriptionResult(
        status="created",
        source=source,
        fingerprint="abc123",
        outputs=outputs,
        segment_count=402,
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "audio_transcriber.cli.TranscriptionPipeline.transcribe",
        lambda self, audio, **kwargs: result,
    )

    assert main(["transcribe", str(source)]) == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.out.splitlines() == [
        "status: created",
        f"source: {source}",
        "fingerprint: abc123",
        "segment_count: 402",
        "outputs:",
        f"  markdown: {outputs.markdown}",
        f"  srt: {outputs.srt}",
        f"  jsonl: {outputs.jsonl}",
        f"  manifest: {outputs.manifest}",
    ]
    assert "Unexpected failure" not in captured.err


def test_transcribe_dry_run_prints_plan_as_text(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")

    assert main(["transcribe", str(source), "--dry-run"]) == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.out.startswith(f"status: planned\nsource: {source}\n")
    assert f"  markdown: {tmp_path / 'audio__local__local-small_transcript.md'}\n" in (
        captured.out
    )
    assert "plan:\n  mode: local\n  profile: local/local-small\n" in captured.out
    assert "  network_calls: 0\n" in captured.out
    assert f"[INFO] Hashing source media: {source}" in captured.err


def test_config_list_outputs_machine_readable_json(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    assert main(["config", "list", "--json"]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["schema_version"] == "3.0.0"
    assert payload["effective_schema_version"] == "3.0.0"
    assert payload["has_in_memory_migrations"] is False
    assert payload["active_mode"] == "local"
    assert payload["active_profile"] == "local-small"
    assert payload["config_sources"][0] == {
        "kind": "built_in",
        "path": None,
        "exists": True,
        "schema_version": "3.0.0",
        "effective_schema_version": "3.0.0",
        "migration": None,
    }
    assert payload["values"]["model"]["value"] == "small"
    assert payload["values"]["output_dir"]["value"] == str(tmp_path.resolve())
    assert payload["values"]["output_dir"]["source"] == "built-in default"
    assert captured.err == "[INFO] Showing resolved configuration\n"


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("verbosity", [[], ["--quiet"], ["--verbose"]])
def test_config_list_is_read_only_with_sources_and_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
    verbosity: list[str],
) -> None:
    home = tmp_path / "home"
    user = home / ".tkn" / "audio_transcriber" / "config.yaml"
    working = tmp_path / ".tkn" / "config.yaml"
    explicit = tmp_path / "explicit.yaml"
    user.parent.mkdir(parents=True)
    working.parent.mkdir(parents=True)
    user.write_text(
        'schema_version: "3.0.0"\nprocessing:\n  heartbeat_seconds: 7\n', encoding="utf-8"
    )
    working.write_text(
        'schema_version: "3.0.0"\nprocessing:\n  heartbeat_seconds: 8\n', encoding="utf-8"
    )
    explicit.write_text(
        'schema_version: "3.0.0"\nprocessing:\n  heartbeat_seconds: 9\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)

    def snapshot() -> dict[Path, bytes | None]:
        return {
            path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
            for path in tmp_path.rglob("*")
        }

    before = snapshot()
    args = [
        *verbosity,
        "--config",
        str(explicit),
        "--profile",
        "local/local-large",
        "config",
        "list",
    ]
    if json_output:
        args.append("--json")
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == (
        "" if "--quiet" in verbosity else "[INFO] Showing resolved configuration\n"
    )
    if json_output:
        payload = json.loads(captured.out)
        assert payload["active_profile"] == "local-large"
        assert payload["active_profile_source"] == "CLI option"
        assert payload["values"]["heartbeat_seconds"] == {
            "value": 9,
            "source": f"explicit config: {explicit}",
        }
        assert payload["loaded_files"] == [str(user), str(working), str(explicit)]
    else:
        lines = captured.out.splitlines()
        assert all("=" in line for line in lines)
        assert "active_profile=local-large" in lines
        assert "active_profile_source=CLI option" in lines
        assert "values.heartbeat_seconds.value=9" in lines
        assert f"values.heartbeat_seconds.source=explicit config: {explicit}" in lines
        assert "config_sources[3].schema_version=3.0.0" in lines
        assert f"loaded_files[0]={user}" in lines
        assert f"loaded_files[1]={working}" in lines
        assert f"loaded_files[2]={explicit}" in lines
    assert snapshot() == before


def test_config_lines_preserve_paths_and_escape_control_characters() -> None:
    assert _config_lines(
        {
            "path": r"C:\Users\ExampleUser\My Documents\音声",
            "items": [{"active": True}, False, None],
            "empty_list": [],
            "empty_mapping": {},
            "count": 42,
            "text": "a=b\r\n\tc\x00",
        }
    ) == [
        r"path=C:\Users\ExampleUser\My Documents\音声",
        "items[0].active=true",
        "items[1]=false",
        "items[2]=null",
        "empty_list=[]",
        "empty_mapping={}",
        "count=42",
        r"text=a=b\r\n\tc\x00",
    ]


def test_config_list_without_config_does_not_create_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "missing-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)
    assert main(["config", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "has_in_memory_migrations=false" in lines
    assert "config_sources[0].path=null" in lines
    assert "config_sources[1].exists=false" in lines
    assert "loaded_files=[]" in lines
    assert not list(tmp_path.iterdir())


def test_config_show_is_removed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["config", "show"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid choice" in captured.err


def test_config_list_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["config", "list", "--help"])
    assert exc.value.code == 0
    assert "--json" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_config_list_missing_explicit_config_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert main(["--config", "missing.yaml", "config", "list", *extra]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[ERROR] Explicit config file not found:" in captured.err
    assert not list(tmp_path.iterdir())


def test_config_profiles_lists_and_selects_profiles(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert main(["--profile", "local/local-large", "config", "profiles"]) == 0

    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert payload["active_mode"] == "local"
    assert payload["active_profile"] == "local-large"
    assert payload["active_profile_source"] == "CLI option"
    assert {profile["selector"] for profile in payload["profiles"]} == {
        "local/local-small",
        "local/local-large",
        "local/gpu-quality",
        "local/gpu-fast",
        "cloud/azure-ja",
    }
    assert next(
        profile
        for profile in payload["profiles"]
        if profile["selector"] == "local/local-large"
    )["active"] is True


def test_config_list_warns_for_legacy_integer_schema(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    config = tmp_path / "legacy.yaml"
    config.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert main(["--config", str(config), "config", "list", "--json"]) == 0

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["has_in_memory_migrations"] is True
    assert payload["config_sources"][-1]["schema_version"] == 1
    assert "[WARNING]" in captured.err
    assert "config migrate" in captured.err


def test_config_init_and_migrate_commands(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    target = tmp_path / "config.yaml"
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert main(["config", "init", str(target)]) == 0
    initialized = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert initialized["status"] == "created"
    assert target.read_text(encoding="utf-8").startswith('schema_version: "3.0.0"')

    target.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")
    assert main(["config", "migrate", str(target), "--dry-run"]) == 0
    planned = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert planned["status"] == "planned"
    assert target.read_text(encoding="utf-8").startswith("schema_version: 1\n")

    assert main(["config", "migrate", str(target)]) == 0
    migrated = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert migrated["status"] == "migrated"
    assert target.read_text(encoding="utf-8").startswith('schema_version: "3.0.0"')
    assert Path(migrated["backup_path"]).is_file()


def test_transcribe_dry_run_defaults_output_to_current_working_directory(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    assert main(["transcribe", str(source), "--dry-run", "--json"]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["status"] == "planned"
    assert Path(payload["outputs"]["markdown"]).parent == tmp_path.resolve()
    assert payload["outputs"]["markdown"].endswith("audio__local__local-small_transcript.md")
    assert payload["plan"]["provider"] == "faster-whisper"


def test_profile_switches_transcription_model_for_dry_run(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert (
        main(
            ["--profile", "local/local-large", "transcribe", str(source), "--dry-run", "--json"]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert payload["status"] == "planned"
    assert payload["plan"]["mode"] == "local"
    assert payload["plan"]["profile"] == "local/local-large"
    assert payload["plan"]["provider"] == "faster-whisper"
    assert payload["outputs"]["markdown"].endswith("audio__local__local-large_transcript.md")


def test_cuda_preflight_failure_is_an_actionable_cli_error(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    def reject_cuda(device: str) -> None:
        raise ValidationError(
            "CUDA GPU runtime preflight failed before audio processing: test failure"
        )

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "audio_transcriber.pipeline.validate_cuda_runtime", reject_cuda
    )

    assert (
        main(["--profile", "local/gpu-quality", "transcribe", str(source)]) == 2
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.out == ""
    assert "[ERROR] CUDA GPU runtime preflight failed" in captured.err
    assert "Unexpected failure" not in captured.err


def test_azure_profile_dry_run_does_not_require_upload_approval(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    config = tmp_path / "azure.yaml"
    config.write_text(
        'schema_version: "3.0.0"\n'
        "transcription:\n"
        "  cloud:\n"
        "    profiles:\n"
        "      azure-ja:\n"
        "        endpoint: https://example.cognitiveservices.azure.com/\n",
        encoding="utf-8",
    )
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert (
        main(
            [
                "--config",
                str(config),
                "--profile",
                "cloud/azure-ja",
                "transcribe",
                str(source),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert payload["status"] == "planned"
    assert payload["plan"]["mode"] == "cloud"
    assert payload["plan"]["profile"] == "cloud/azure-ja"
    assert payload["plan"]["provider"] == "azure-speech-fast"
    assert payload["plan"]["authentication_method"] == "InteractiveBrowserCredential"
    assert payload["plan"]["account_selection_required"] is False
    assert payload["plan"]["authentication_interaction"] == "if_required"
    assert payload["plan"]["cloud_upload_approval_required"] is True
    assert payload["plan"]["network_calls"] == 0
    assert payload["plan"]["normalized_audio_validation"]["status"] == (
        "deferred_until_actual_run"
    )

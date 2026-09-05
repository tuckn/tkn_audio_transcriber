import json
from pathlib import Path

from audio_transcriber.cli import main
from audio_transcriber.errors import ValidationError


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
    assert "browser sign-in and account selection" in " ".join(captured.out.split())


def test_config_show_outputs_machine_readable_json(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    assert main(["config", "show"]) == 0
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
    assert captured.err == ""


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


def test_config_show_warns_for_legacy_integer_schema(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    config = tmp_path / "legacy.yaml"
    config.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert main(["--config", str(config), "config", "show"]) == 0

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
    assert main(["transcribe", str(source), "--dry-run"]) == 0
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
            ["--profile", "local/local-large", "transcribe", str(source), "--dry-run"]
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
    assert payload["plan"]["account_selection_required"] is True
    assert payload["plan"]["cloud_upload_approval_required"] is True
    assert payload["plan"]["network_calls"] == 0
    assert payload["plan"]["normalized_audio_validation"]["status"] == (
        "deferred_until_actual_run"
    )

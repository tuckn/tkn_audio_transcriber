import json
from pathlib import Path

from audio_transcriber.cli import main


def test_help_and_version(capsys: object) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0


def test_transcribe_help_describes_audio_and_video(capsys: object) -> None:
    try:
        main(["transcribe", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "Source audio or video file with an audio stream" in captured.out


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
    assert payload["schema_version"] == "1.1.0"
    assert payload["effective_schema_version"] == "1.1.0"
    assert payload["has_in_memory_migrations"] is False
    assert payload["config_sources"][0] == {
        "kind": "built_in",
        "path": None,
        "exists": True,
        "schema_version": "1.1.0",
        "effective_schema_version": "1.1.0",
        "migration": None,
    }
    assert payload["values"]["model"]["value"] == "small"
    assert payload["values"]["output_dir"]["value"] == str(tmp_path.resolve())
    assert payload["values"]["output_dir"]["source"] == "built-in default"
    assert captured.err == ""


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
    assert target.read_text(encoding="utf-8").startswith('schema_version: "1.1.0"')

    target.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")
    assert main(["config", "migrate", str(target), "--dry-run"]) == 0
    planned = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert planned["status"] == "planned"
    assert target.read_text(encoding="utf-8").startswith("schema_version: 1\n")

    assert main(["config", "migrate", str(target)]) == 0
    migrated = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert migrated["status"] == "migrated"
    assert target.read_text(encoding="utf-8").startswith('schema_version: "1.1.0"')
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
    assert payload["plan"]["provider"] == "faster-whisper"


def test_azure_dry_run_does_not_require_upload_approval(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    config = tmp_path / "azure.yaml"
    config.write_text(
        'schema_version: "1.1.0"\n'
        'provider: azure-speech-fast\n'
        'azure_speech_endpoint: https://example.cognitiveservices.azure.com/\n',
        encoding="utf-8",
    )
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.setenv("HOME", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.setenv("USERPROFILE", str(isolated_home))  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    assert main(["--config", str(config), "transcribe", str(source), "--dry-run"]) == 0

    payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert payload["status"] == "planned"
    assert payload["plan"]["provider"] == "azure-speech-fast"
    assert payload["plan"]["cloud_upload_approval_required"] is True
    assert payload["plan"]["network_calls"] == 0
    assert payload["plan"]["normalized_audio_validation"]["status"] == (
        "deferred_until_actual_run"
    )

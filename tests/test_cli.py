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
    assert payload["schema_version"] == 1
    assert payload["values"]["model"]["value"] == "small"
    assert payload["values"]["output_dir"]["value"] == str(tmp_path.resolve())
    assert payload["values"]["output_dir"]["source"] == "built-in default"
    assert captured.err == ""


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

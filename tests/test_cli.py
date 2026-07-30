import json
from pathlib import Path

from audio_transcriber.cli import main


def test_help_and_version(capsys: object) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0


def test_config_show_outputs_machine_readable_json(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    assert main(["config", "show"]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["values"]["model"]["value"] == "small"
    assert captured.err == ""


def test_transcribe_requires_output_directory(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    assert main(["transcribe", str(source), "--dry-run"]) == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "output_dir is required" in captured.err
    assert captured.out == ""


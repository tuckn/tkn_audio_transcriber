from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from audio_transcriber.ffmpeg_adapter import FfmpegAdapter


def test_normalize_extracts_first_audio_stream_from_mp4(
    tmp_path: Path, monkeypatch: object
) -> None:
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"synthetic video container")
    destination = tmp_path / "normalized.wav"
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"synthetic normalized audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)  # type: ignore[attr-defined]
    adapter = FfmpegAdapter("ffmpeg", 30, logging.getLogger("test.ffmpeg"))

    adapter.normalize(source, destination)

    assert destination.is_file()
    assert commands == [
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-vn",
            str(destination),
        ]
    ]

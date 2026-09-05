from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from audio_transcriber.errors import ExternalProcessError
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


@pytest.mark.parametrize("create_empty_file", [False, True])
def test_flac_encoding_requires_nonempty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create_empty_file: bool
) -> None:
    destination = tmp_path / "upload.flac"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if create_empty_file:
            destination.touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = FfmpegAdapter("ffmpeg", 30, logging.getLogger("test.ffmpeg"))
    with pytest.raises(ExternalProcessError, match="FLAC upload file"):
        adapter.encode_flac(tmp_path / "normalized.wav", destination)


def test_real_flac_encoding_preserves_pcm_samples(tmp_path: Path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("ffmpeg is required for the lossless FLAC round-trip test")
    normalized = tmp_path / "normalized.wav"
    compressed = tmp_path / "upload.flac"
    samples = b"".join(
        struct.pack("<h", round(12000 * math.sin(2 * math.pi * 440 * n / 16000)))
        for n in range(16000)
    )
    with wave.open(str(normalized), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(samples)

    adapter = FfmpegAdapter(executable, 30, logging.getLogger("test.ffmpeg"))
    adapter.encode_flac(normalized, compressed)

    assert compressed.read_bytes().startswith(b"fLaC")
    assert compressed.stat().st_size < normalized.stat().st_size
    decoded = tmp_path / "decoded.wav"
    subprocess.run(
        [executable, "-nostdin", "-v", "error", "-i", str(compressed), str(decoded)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with wave.open(str(decoded), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 16000
        assert stream.getnframes() == 16000
        assert stream.readframes(stream.getnframes()) == samples

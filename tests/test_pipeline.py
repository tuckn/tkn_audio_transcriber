from __future__ import annotations

import json
import logging
import wave
from pathlib import Path

import pytest

from audio_transcriber.config import ResolvedConfig, resolve_config
from audio_transcriber.errors import ValidationError
from audio_transcriber.io_utils import sha256_file
from audio_transcriber.models import Segment
from audio_transcriber.pipeline import TranscriptionPipeline
from audio_transcriber.validation import validate_artifact


class FakeFfmpeg:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def ensure_available(self) -> None:
        self.calls.append("ensure")

    def normalize(self, source: Path, destination: Path) -> None:
        self.calls.append("normalize")
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(destination, seconds=20)

    def split(self, normalized: Path, chunk_dir: Path, chunk_seconds: int) -> list[Path]:
        self.calls.append("split")
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunks = [chunk_dir / "chunk_000000.wav", chunk_dir / "chunk_000001.wav"]
        for chunk in chunks:
            _write_wav(chunk, seconds=10)
        return chunks


def _write_wav(path: Path, *, seconds: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 16_000 * seconds)


class FakeRecognizer:
    def __init__(self, calls: list[str], fail_on: str | None = None) -> None:
        self.calls = calls
        self.fail_on = fail_on

    def transcribe_chunk(
        self, chunk: Path, *, offset: float, first_index: int
    ) -> tuple[list[Segment], str, float]:
        self.calls.append(chunk.name)
        if chunk.name == self.fail_on:
            raise RuntimeError("simulated interruption")
        return (
            [
                Segment(
                    index=first_index,
                    start=offset + 0.1,
                    end=offset + 1.2,
                    text=f"text from {chunk.name}",
                    chunk=chunk.name,
                )
            ],
            "ja",
            0.99,
        )


def make_config(tmp_path: Path) -> ResolvedConfig:
    model_dir = tmp_path / "models"
    model = model_dir / "faster-whisper-small"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    return resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        cli_overrides={
            "output_dir": str(tmp_path / "outputs"),
            "state_dir": str(tmp_path / "state"),
            "model_dir": str(model_dir),
            "cache_dir": str(tmp_path / "cache"),
            "chunk_seconds": 10,
        },
    )


def make_logger() -> logging.Logger:
    logger = logging.getLogger("audio_transcriber.tests")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def test_dry_run_changes_nothing_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "meeting.flac"
    source.write_bytes(b"immutable source")
    before = sha256_file(source)
    calls: list[str] = []
    pipeline = TranscriptionPipeline(
        config=make_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(calls),  # type: ignore[arg-type]
    )
    result = pipeline.transcribe(source, dry_run=True, overwrite=False)
    assert result.status == "planned"
    assert calls == []
    assert sha256_file(source) == before
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "state").exists()


def test_end_to_end_commit_validate_unchanged_and_source_immutability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "会議.flac"
    source.write_bytes(b"immutable source")
    before = sha256_file(source)
    ffmpeg_calls: list[str] = []
    recognition_calls: list[str] = []
    config = make_config(tmp_path)
    pipeline = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer(recognition_calls),
    )
    result = pipeline.transcribe(source, dry_run=False, overwrite=False)
    assert result.status == "created"
    assert result.segment_count == 2
    assert sha256_file(source) == before
    assert result.outputs.markdown.is_file()
    assert result.outputs.srt.is_file()
    assert result.outputs.jsonl.is_file()
    assert result.outputs.manifest.is_file()
    assert validate_artifact(result.outputs.manifest, verify_source=True)["status"] == "valid"
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    assert manifest["decoded_audio"]["duration_seconds"] == 20.0
    job_files = list((tmp_path / "state" / "jobs").glob("*/job.json"))
    assert json.loads(job_files[0].read_text(encoding="utf-8"))["status"] == "completed"

    second = pipeline.transcribe(source, dry_run=False, overwrite=False)
    assert second.status == "unchanged"
    assert recognition_calls == ["chunk_000000.wav", "chunk_000001.wav"]


def test_resume_skips_completed_chunks(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    config = make_config(tmp_path)
    first_calls: list[str] = []
    first = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer(
            first_calls, fail_on="chunk_000001.wav"
        ),
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        first.transcribe(source, dry_run=False, overwrite=False)
    assert first_calls == ["chunk_000000.wav", "chunk_000001.wav"]
    failed_job = next((tmp_path / "state" / "jobs").glob("*/job.json"))
    assert json.loads(failed_job.read_text(encoding="utf-8"))["status"] == "failed"

    resumed_calls: list[str] = []
    resumed = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer(resumed_calls),
    )
    result = resumed.transcribe(source, dry_run=False, overwrite=False)
    assert result.status == "created"
    assert resumed_calls == ["chunk_000001.wav"]
    assert result.segment_count == 2


def test_different_existing_output_requires_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source one")
    config = make_config(tmp_path)
    make = lambda: TranscriptionPipeline(  # noqa: E731
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    )
    make().transcribe(source, dry_run=False, overwrite=False)
    source.write_bytes(b"source two")
    with pytest.raises(ValidationError, match="--overwrite"):
        make().transcribe(source, dry_run=False, overwrite=False)
    replaced = make().transcribe(source, dry_run=False, overwrite=True)
    assert replaced.status == "replaced"


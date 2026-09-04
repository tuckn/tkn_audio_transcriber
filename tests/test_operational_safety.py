from __future__ import annotations

import json
import logging
import time
import wave
from pathlib import Path

import pytest

from audio_transcriber.audio_integrity import (
    AZURE_MAX_FILE_BYTES,
    WavInfo,
    inspect_normalized_wav,
    validate_azure_upload_limits,
    validate_chunk_coverage,
)
from audio_transcriber.errors import ValidationError
from audio_transcriber.io_utils import atomic_write_json, sha256_file
from audio_transcriber.job_state import JobTracker, list_jobs
from audio_transcriber.maintenance import cleanup_jobs


def _write_wav(path: Path, *, seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 16_000 * seconds)


def test_chunk_duration_mismatch_is_rejected(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.wav"
    chunk = tmp_path / "chunk.wav"
    _write_wav(normalized, seconds=10)
    _write_wav(chunk, seconds=2)

    with pytest.raises(ValidationError, match="Chunk duration does not match"):
        validate_chunk_coverage(inspect_normalized_wav(normalized), [chunk])


def test_azure_upload_size_limit_is_strict(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.wav"
    with normalized.open("wb") as stream:
        stream.seek(AZURE_MAX_FILE_BYTES - 1)
        stream.write(b"\0")
    info = WavInfo(
        duration_seconds=1.0,
        frame_count=16_000,
        sample_rate=16_000,
        channels=1,
        sample_width=2,
    )

    with pytest.raises(ValidationError, match="smaller than 250 MB"):
        validate_azure_upload_limits(normalized, info)


def test_heartbeat_run_log_and_status_are_durable(tmp_path: Path) -> None:
    tracker = JobTracker(
        job_dir=tmp_path / "jobs" / "meeting-123",
        fingerprint="123",
        source={"path": "meeting.wav", "size": 1, "sha256": "abc"},
        settings={"model": "small"},
        heartbeat_seconds=1,
        logger=logging.getLogger("audio_transcriber.tests.heartbeat"),
    )
    tracker.start()
    tracker.set_stage(
        "transcribing",
        current_chunk="chunk_000000.wav",
        chunk_position=1,
        chunk_count=1,
    )
    tracker.run_with_heartbeat(lambda: time.sleep(1.1))
    tracker.checkpoint()
    tracker.complete(tmp_path / "transcript.manifest.json")

    status = list_jobs(tmp_path)
    assert status["job_count"] == 1
    assert status["jobs"][0]["status"] == "completed"
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "jobs" / "meeting-123" / "run.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert "heartbeat" in events
    assert events[-1] == "completed"

def test_cleanup_requires_apply_and_preserves_non_completed_jobs(tmp_path: Path) -> None:
    output = tmp_path / "outputs" / "transcript.md"
    output.parent.mkdir()
    output.write_text("transcript\n", encoding="utf-8")
    manifest = output.with_suffix(".manifest.json")
    atomic_write_json(
        manifest,
        {
            "schema_version": 1,
            "source": {"path": "missing.wav", "sha256": "not-verified"},
            "outputs": {
                "markdown": {
                    "path": str(output),
                    "size": output.stat().st_size,
                    "sha256": sha256_file(output),
                }
            },
        },
    )
    completed = tmp_path / "state" / "jobs" / "completed"
    failed = tmp_path / "state" / "jobs" / "failed"
    atomic_write_json(
        completed / "job.json",
        {
            "status": "completed",
            "output_manifest": str(manifest),
        },
    )
    atomic_write_json(failed / "job.json", {"status": "failed"})
    (completed / "segments.jsonl").write_text("{}\n", encoding="utf-8")

    planned = cleanup_jobs(tmp_path / "state", older_than_days=0, apply=False)
    assert planned["status"] == "planned"
    assert planned["candidate_count"] == 1
    assert completed.is_dir()
    assert failed.is_dir()

    applied = cleanup_jobs(tmp_path / "state", older_than_days=0, apply=True)
    assert applied["deleted_count"] == 1
    assert not completed.exists()
    assert failed.is_dir()

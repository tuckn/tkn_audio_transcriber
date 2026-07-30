from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

from .io_utils import atomic_write_json, read_json

ResultT = TypeVar("ResultT")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobTracker:
    def __init__(
        self,
        *,
        job_dir: Path,
        fingerprint: str,
        source: dict[str, Any],
        settings: dict[str, Any],
        heartbeat_seconds: int,
        logger: logging.Logger,
    ) -> None:
        self.job_path = job_dir / "job.json"
        self.run_log_path = job_dir / "run.jsonl"
        self.fingerprint = fingerprint
        self.source = source
        self.settings = settings
        self.heartbeat_seconds = heartbeat_seconds
        self.logger = logger
        self.started_at = now_iso()
        self.stage = "initializing"
        self.current_chunk: str | None = None
        self.chunk_position: int | None = None
        self.chunk_count: int | None = None
        self.last_checkpoint_at: str | None = None
        self.output_manifest: str | None = None
        self._lock = threading.Lock()

    def _payload(self, *, status: str, detail: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "settings": self.settings,
            "status": status,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "heartbeat_at": now_iso(),
            "heartbeat_seconds": self.heartbeat_seconds,
            "stage": self.stage,
            "current_chunk": self.current_chunk,
            "chunk_position": self.chunk_position,
            "chunk_count": self.chunk_count,
            "last_checkpoint_at": self.last_checkpoint_at,
            "run_log": str(self.run_log_path),
        }
        if self.output_manifest is not None:
            payload["output_manifest"] = self.output_manifest
        if detail is not None:
            payload["detail"] = detail
        return payload

    def _write(self, *, status: str, event: str, detail: str | None = None) -> None:
        with self._lock:
            payload = self._payload(status=status, detail=detail)
            atomic_write_json(self.job_path, payload)
            self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.run_log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(
                        {
                            "timestamp": payload["heartbeat_at"],
                            "event": event,
                            "status": status,
                            "stage": self.stage,
                            "current_chunk": self.current_chunk,
                            "chunk_position": self.chunk_position,
                            "chunk_count": self.chunk_count,
                            "detail": detail,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())

    def start(self) -> None:
        self._write(status="running", event="started")

    def set_stage(
        self,
        stage: str,
        *,
        current_chunk: str | None = None,
        chunk_position: int | None = None,
        chunk_count: int | None = None,
    ) -> None:
        self.stage = stage
        self.current_chunk = current_chunk
        self.chunk_position = chunk_position
        self.chunk_count = chunk_count
        self._write(status="running", event="stage")

    def checkpoint(self) -> None:
        self.last_checkpoint_at = now_iso()
        self._write(status="running", event="checkpoint")

    def heartbeat(self, *, elapsed_seconds: float) -> None:
        detail = f"elapsed_seconds={elapsed_seconds:.1f}"
        self._write(status="running", event="heartbeat", detail=detail)
        self.logger.info(
            "Still transcribing %s (%d/%d); elapsed %.0fs",
            self.current_chunk,
            self.chunk_position or 0,
            self.chunk_count or 0,
            elapsed_seconds,
        )

    def run_with_heartbeat(self, action: Callable[[], ResultT]) -> ResultT:
        stop = threading.Event()
        started = monotonic()

        def emit() -> None:
            while not stop.wait(self.heartbeat_seconds):
                self.heartbeat(elapsed_seconds=monotonic() - started)

        worker = threading.Thread(target=emit, name="transcription-heartbeat", daemon=True)
        worker.start()
        try:
            return action()
        finally:
            stop.set()
            worker.join(timeout=1)

    def complete(self, output_manifest: Path) -> None:
        self.output_manifest = str(output_manifest)
        self.stage = "completed"
        self.current_chunk = None
        self._write(
            status="completed",
            event="completed",
            detail=f"output_manifest={output_manifest}",
        )

    def fail(self, exc: BaseException) -> None:
        self.stage = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        self._write(status=self.stage, event=self.stage, detail=f"{type(exc).__name__}: {exc}")


def list_jobs(state_dir: Path) -> dict[str, Any]:
    resolved_state = state_dir.expanduser().resolve(strict=False)
    jobs_root = resolved_state / "jobs"
    jobs: list[dict[str, Any]] = []
    if jobs_root.is_dir():
        for job_path in sorted(jobs_root.glob("*/job.json")):
            try:
                payload = read_json(job_path)
            except (OSError, ValueError):
                payload = {"status": "unreadable"}
            payload["job_id"] = job_path.parent.name
            payload["job_directory"] = str(job_path.parent)
            heartbeat_at = payload.get("heartbeat_at")
            heartbeat_seconds = payload.get("heartbeat_seconds")
            if isinstance(heartbeat_at, str) and isinstance(heartbeat_seconds, int):
                try:
                    heartbeat_age = max(
                        0.0,
                        (datetime.now().astimezone() - datetime.fromisoformat(heartbeat_at))
                        .total_seconds(),
                    )
                except ValueError:
                    pass
                else:
                    payload["heartbeat_age_seconds"] = round(heartbeat_age, 1)
                    payload["heartbeat_stale"] = (
                        payload.get("status") == "running"
                        and heartbeat_age > max(heartbeat_seconds * 2, 10)
                    )
            jobs.append(payload)
    return {
        "status": "ok",
        "state_dir": str(resolved_state),
        "job_count": len(jobs),
        "jobs": jobs,
    }
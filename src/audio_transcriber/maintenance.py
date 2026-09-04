from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io_utils import read_json
from .validation import validate_artifact


def _directory_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _job_time(payload: dict[str, Any], job_path: Path) -> datetime:
    for key in ("heartbeat_at", "completed_at"):
        raw = payload.get(key)
        if isinstance(raw, str):
            try:
                value = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if value.tzinfo is not None:
                return value.astimezone()
    return datetime.fromtimestamp(job_path.stat().st_mtime).astimezone()


def cleanup_jobs(
    state_dir: Path,
    *,
    older_than_days: int,
    apply: bool,
) -> dict[str, Any]:
    if older_than_days < 0:
        raise ValidationError("older_than_days must be zero or greater")
    resolved_state = state_dir.expanduser().resolve(strict=False)
    jobs_root = (resolved_state / "jobs").resolve(strict=False)
    cutoff = datetime.now().astimezone() - timedelta(days=older_than_days)
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    if jobs_root.is_dir():
        for candidate in sorted(jobs_root.iterdir()):
            job_dir = candidate.resolve(strict=False)
            job_id = candidate.name
            if not candidate.is_dir() or job_dir.parent != jobs_root:
                skipped.append({"job_id": job_id, "reason": "unsafe job path"})
                continue
            job_path = job_dir / "job.json"
            try:
                payload = read_json(job_path)
            except (OSError, ValueError) as exc:
                skipped.append({"job_id": job_id, "reason": f"unreadable job state: {exc}"})
                continue
            if payload.get("status") != "completed":
                skipped.append({"job_id": job_id, "reason": "job is not completed"})
                continue
            try:
                job_time = _job_time(payload, job_path)
            except OSError as exc:
                skipped.append({"job_id": job_id, "reason": f"cannot read job time: {exc}"})
                continue
            if job_time > cutoff:
                skipped.append({"job_id": job_id, "reason": "job is newer than retention"})
                continue
            raw_manifest = payload.get("output_manifest")
            if not isinstance(raw_manifest, str):
                skipped.append({"job_id": job_id, "reason": "output manifest is not recorded"})
                continue
            manifest = Path(raw_manifest).expanduser().resolve(strict=False)
            try:
                validate_artifact(manifest, verify_source=False)
            except (OSError, ValueError, ValidationError) as exc:
                skipped.append({"job_id": job_id, "reason": f"output validation failed: {exc}"})
                continue
            candidates.append(
                {
                    "job_id": job_id,
                    "job_directory": str(job_dir),
                    "bytes": _directory_size(job_dir),
                    "output_manifest": str(manifest),
                }
            )

    deleted: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if apply:
        for job_record in candidates:
            job_dir = Path(str(job_record["job_directory"])).resolve(strict=False)
            if job_dir.parent != jobs_root:
                failures.append(
                    {"job_id": str(job_record["job_id"]), "reason": "unsafe job path"}
                )
                continue
            try:
                shutil.rmtree(job_dir)
            except OSError as exc:
                failures.append({"job_id": str(job_record["job_id"]), "reason": str(exc)})
            else:
                deleted.append(job_record)

    return {
        "status": "applied" if apply else "planned",
        "state_dir": str(resolved_state),
        "older_than_days": older_than_days,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(item["bytes"]) for item in candidates),
        "candidates": candidates,
        "deleted_count": len(deleted),
        "deleted_bytes": sum(int(item["bytes"]) for item in deleted),
        "deleted": deleted,
        "skipped": skipped,
        "failures": failures,
    }

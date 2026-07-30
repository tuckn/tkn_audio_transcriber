from __future__ import annotations

import re
from pathlib import Path

from .models import OutputPaths


def safe_stem(source: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._")
    return value or "audio"


def output_paths(output_dir: Path, source: Path) -> OutputPaths:
    stem = safe_stem(source)
    return OutputPaths(
        markdown=output_dir / f"{stem}_transcript.md",
        srt=output_dir / f"{stem}_transcript.srt",
        jsonl=output_dir / f"{stem}_transcript.jsonl",
        manifest=output_dir / f"{stem}_transcript.manifest.json",
    )


def job_directory(state_dir: Path, source: Path, fingerprint: str) -> Path:
    return state_dir / "jobs" / f"{safe_stem(source)}-{fingerprint[:12]}"


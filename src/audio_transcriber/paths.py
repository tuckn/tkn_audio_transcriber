from __future__ import annotations

import re
from pathlib import Path

from .models import OutputPaths


def safe_stem(source: Path) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._")
    return value or "audio"


def safe_profile_component(value: str) -> str:
    component = re.sub(r"[^\w.-]+", "_", value).strip("._")
    return component or "profile"


def output_paths(output_dir: Path, source: Path, *, mode: str, profile: str) -> OutputPaths:
    stem = (
        f"{safe_stem(source)}__{safe_profile_component(mode)}__"
        f"{safe_profile_component(profile)}_transcript"
    )
    return OutputPaths(
        markdown=output_dir / f"{stem}.md",
        srt=output_dir / f"{stem}.srt",
        jsonl=output_dir / f"{stem}.jsonl",
        manifest=output_dir / f"{stem}.manifest.json",
    )


def job_directory(state_dir: Path, source: Path, fingerprint: str) -> Path:
    return state_dir / "jobs" / f"{safe_stem(source)}-{fingerprint[:12]}"

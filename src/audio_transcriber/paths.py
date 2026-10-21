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


def media_stem(source: Path) -> str:
    # Keep Unicode and spaces, but make the component portable to Windows.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source.stem).strip(" .")
    device = value.split(".")[0].upper()
    if device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(
        r"(?:COM|LPT)[1-9¹²³]", device
    ):
        value = "_" + value
    # Leave space for the hash suffix and transcript filenames on Windows.
    shortened = value.encode("utf-16-le")[:120].decode("utf-16-le", errors="ignore")
    return shortened.rstrip(" .") or "audio"


def output_paths(
    output_dir: Path, source: Path, *, mode: str, profile: str,
    source_hash: str, fingerprint: str,
) -> OutputPaths:
    directory = output_dir / f"{media_stem(source)}_{source_hash[:12]}"
    stem = (
        f"transcript_{safe_profile_component(mode)}_"
        f"{safe_profile_component(profile)[:40]}_{fingerprint[:12]}"
    )
    return OutputPaths(
        audio=directory / "audio.flac",
        markdown=directory / f"{stem}.md",
        srt=directory / f"{stem}.srt",
        jsonl=directory / f"{stem}.jsonl",
        manifest=directory / f"{stem}.manifest.json",
    )


def job_directory(state_dir: Path, source: Path, fingerprint: str) -> Path:
    return state_dir / "jobs" / f"{safe_stem(source)}-{fingerprint[:12]}"

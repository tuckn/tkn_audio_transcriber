from __future__ import annotations

import shutil
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError

NORMALIZED_SAMPLE_RATE = 16_000
NORMALIZED_CHANNELS = 1
NORMALIZED_SAMPLE_WIDTH = 2
MINIMUM_PREFLIGHT_BYTES = 512 * 1024 * 1024
POST_NORMALIZE_MARGIN_BYTES = 64 * 1024 * 1024
AZURE_MAX_DURATION_SECONDS = 2 * 60 * 60
AZURE_MAX_FILE_BYTES = 250_000_000


@dataclass(frozen=True)
class WavInfo:
    duration_seconds: float
    frame_count: int
    sample_rate: int
    channels: int
    sample_width: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_normalized_wav(path: Path) -> WavInfo:
    try:
        with wave.open(str(path), "rb") as stream:
            frame_count = stream.getnframes()
            sample_rate = stream.getframerate()
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError(f"Normalized WAV is unreadable or incomplete: {path}: {exc}") from exc

    if sample_rate != NORMALIZED_SAMPLE_RATE:
        raise ValidationError(
            f"Normalized WAV sample rate is {sample_rate}, "
            f"expected {NORMALIZED_SAMPLE_RATE}: {path}"
        )
    if channels != NORMALIZED_CHANNELS:
        raise ValidationError(
            f"Normalized WAV has {channels} channels, expected {NORMALIZED_CHANNELS}: {path}"
        )
    if sample_width != NORMALIZED_SAMPLE_WIDTH:
        raise ValidationError(
            f"Normalized WAV sample width is {sample_width}, "
            f"expected {NORMALIZED_SAMPLE_WIDTH}: {path}"
        )
    if frame_count <= 0:
        raise ValidationError(f"Normalized WAV contains no decodable audio frames: {path}")
    return WavInfo(
        duration_seconds=frame_count / sample_rate,
        frame_count=frame_count,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )


def validate_chunk_coverage(normalized: WavInfo, chunks: list[Path]) -> float:
    chunk_duration = sum(inspect_normalized_wav(chunk).duration_seconds for chunk in chunks)
    tolerance = max(1.0, len(chunks) * 0.05)
    difference = abs(chunk_duration - normalized.duration_seconds)
    if difference > tolerance:
        raise ValidationError(
            "Chunk duration does not match normalized audio duration: "
            f"normalized={normalized.duration_seconds:.3f}s, "
            f"chunks={chunk_duration:.3f}s, tolerance={tolerance:.3f}s"
        )
    return chunk_duration


def validate_azure_audio_duration(info: WavInfo) -> None:
    if info.duration_seconds >= AZURE_MAX_DURATION_SECONDS:
        raise ValidationError(
            "Azure Speech normalized audio must be shorter than 2 hours: "
            f"decoded={info.duration_seconds:.3f}s"
        )


def validate_azure_upload_limits(path: Path, info: WavInfo) -> None:
    validate_azure_audio_duration(info)
    size = path.stat().st_size
    if size >= AZURE_MAX_FILE_BYTES:
        raise ValidationError(
            "Azure Speech FLAC upload must be smaller than 250 MB: "
            f"size={size} bytes"
        )


def _existing_anchor(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def ensure_free_space(path: Path, required_bytes: int, *, stage: str) -> int:
    anchor = _existing_anchor(path)
    try:
        free_bytes = shutil.disk_usage(anchor).free
    except OSError as exc:
        raise ValidationError(f"Cannot inspect free disk space for {anchor}: {exc}") from exc
    if free_bytes < required_bytes:
        raise ValidationError(
            f"Insufficient free space for {stage}: "
            f"required at least {required_bytes} bytes, available {free_bytes} bytes on {anchor}"
        )
    return free_bytes


def estimated_initial_scratch_bytes(source_size: int) -> int:
    return max(MINIMUM_PREFLIGHT_BYTES, source_size * 4)


def required_derived_audio_space_bytes(normalized_size: int) -> int:
    return normalized_size + POST_NORMALIZE_MARGIN_BYTES

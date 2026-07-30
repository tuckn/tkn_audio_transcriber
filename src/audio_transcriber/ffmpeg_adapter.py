from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .errors import ExternalProcessError


class FfmpegAdapter:
    def __init__(self, executable: str, timeout_seconds: int, logger: logging.Logger) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.logger = logger

    def ensure_available(self) -> None:
        if Path(self.executable).exists():
            return
        if shutil.which(self.executable) is None:
            raise ExternalProcessError(
                f"ffmpeg executable was not found: {self.executable}. "
                "Install ffmpeg or set ffmpeg_executable."
            )

    def _run(self, arguments: list[str]) -> None:
        command = [self.executable, "-nostdin", "-hide_banner", "-v", "error", *arguments]
        self.logger.debug("External command: %s", command)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExternalProcessError(
                f"ffmpeg timed out after {self.timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise ExternalProcessError(f"Cannot start ffmpeg: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise ExternalProcessError(f"ffmpeg failed: {detail}")

    def normalize(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
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
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ExternalProcessError("ffmpeg did not create a normalized audio file")

    def split(self, normalized: Path, chunk_dir: Path, chunk_seconds: int) -> list[Path]:
        chunk_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "-y",
                "-i",
                str(normalized),
                "-f",
                "segment",
                "-segment_time",
                str(chunk_seconds),
                "-reset_timestamps",
                "1",
                "-c",
                "copy",
                str(chunk_dir / "chunk_%06d.wav"),
            ]
        )
        chunks = sorted(chunk_dir.glob("chunk_*.wav"))
        if not chunks:
            raise ExternalProcessError(f"ffmpeg created no chunks in {chunk_dir}")
        return chunks


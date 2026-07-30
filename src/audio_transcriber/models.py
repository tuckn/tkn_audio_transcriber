from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float
    text: str
    chunk: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutputPaths:
    markdown: Path
    srt: Path
    jsonl: Path
    manifest: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "markdown": str(self.markdown),
            "srt": str(self.srt),
            "jsonl": str(self.jsonl),
            "manifest": str(self.manifest),
        }


@dataclass(frozen=True)
class TranscriptionResult:
    status: str
    source: Path
    fingerprint: str
    outputs: OutputPaths
    segment_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": str(self.source),
            "fingerprint": self.fingerprint,
            "segment_count": self.segment_count,
            "outputs": self.outputs.as_dict(),
        }


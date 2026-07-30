from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import Segment


class SpeechRecognizer(Protocol):
    def transcribe_chunk(
        self, chunk: Path, *, offset: float, first_index: int
    ) -> tuple[list[Segment], str, float]: ...


class FasterWhisperAdapter:
    def __init__(
        self,
        *,
        model_path: Path,
        language: str,
        beam_size: int,
        device: str,
        compute_type: str,
    ) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Run 'uv sync --locked'."
            ) from exc
        self.language = language
        self.beam_size = beam_size
        self.model = WhisperModel(
            str(model_path), device=device, compute_type=compute_type
        )

    def transcribe_chunk(
        self, chunk: Path, *, offset: float, first_index: int
    ) -> tuple[list[Segment], str, float]:
        raw_segments, info = self.model.transcribe(
            str(chunk),
            language=self.language,
            task="transcribe",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=self.beam_size,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
        )
        segments: list[Segment] = []
        next_index = first_index
        for raw in raw_segments:
            text = " ".join(raw.text.strip().split())
            if not text:
                continue
            segments.append(
                Segment(
                    index=next_index,
                    start=offset + float(raw.start),
                    end=offset + float(raw.end),
                    text=text,
                    chunk=chunk.name,
                )
            )
            next_index += 1
        return segments, str(info.language), float(info.language_probability)


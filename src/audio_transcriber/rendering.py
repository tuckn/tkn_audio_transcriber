"""Render transcript segments as the saved Markdown body, SRT, text, and JSONL."""

from __future__ import annotations

import json

from .models import Segment


def format_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, _ = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def segment_text(segment: Segment, *, markdown: bool) -> str:
    if segment.speaker is None:
        return segment.text
    if markdown:
        return f"[Speaker {segment.speaker}] {segment.text}"
    return f"Speaker {segment.speaker}: {segment.text}"


def markdown_transcript_lines(segments: list[Segment]) -> list[str]:
    """The lines under `## Transcript`, one per segment."""
    return [
        f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] "
        f"{segment_text(segment, markdown=True)}"
        for segment in segments
    ]


def render_srt(segments: list[Segment]) -> str:
    blocks = [
        (
            f"{segment.index}\n"
            f"{format_srt_timestamp(segment.start)} --> "
            f"{format_srt_timestamp(segment.end)}\n"
            f"{segment_text(segment, markdown=False)}\n"
        )
        for segment in segments
    ]
    return "\n".join(blocks)


def render_text(segments: list[Segment]) -> str:
    # One recognized phrase per line; speaker labels stay because they are content.
    return "".join(f"{segment_text(segment, markdown=False)}\n" for segment in segments)


def render_jsonl(segments: list[Segment]) -> str:
    return "".join(json.dumps(segment.to_dict(), ensure_ascii=False) + "\n" for segment in segments)

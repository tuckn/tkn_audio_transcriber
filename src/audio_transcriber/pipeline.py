from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from functools import partial
from pathlib import Path

from . import __version__
from .audio_integrity import (
    ensure_free_space,
    estimated_initial_scratch_bytes,
    inspect_normalized_wav,
    required_chunk_space_bytes,
    validate_chunk_coverage,
)
from .config import ResolvedConfig
from .errors import ValidationError
from .ffmpeg_adapter import FfmpegAdapter
from .io_utils import (
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    read_json,
    sha256_file,
)
from .job_state import JobTracker
from .logging_config import log_success
from .model_store import configure_huggingface_cache, resolve_local_model
from .models import Segment, TranscriptionResult
from .paths import job_directory, output_paths
from .validation import MANIFEST_SCHEMA_VERSION, validate_artifact
from .whisper_adapter import FasterWhisperAdapter, SpeechRecognizer

RecognizerFactory = Callable[[Path, ResolvedConfig], SpeechRecognizer]


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


def _fingerprint(source_hash: str, source_size: int, config: ResolvedConfig) -> str:
    settings = {
        key: config.values[key]
        for key in (
            "model",
            "language",
            "chunk_seconds",
            "beam_size",
            "compute_type",
            "device",
        )
    }
    payload = {
        "source_sha256": source_hash,
        "source_size": source_size,
        "tool_version": __version__,
        "settings": settings,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_segments(path: Path) -> list[Segment]:
    if not path.exists():
        return []
    segments: list[Segment] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            segments.append(
                Segment(
                    index=int(value["index"]),
                    start=float(value["start"]),
                    end=float(value["end"]),
                    text=str(value["text"]),
                    chunk=str(value["chunk"]),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"Invalid checkpoint JSONL at {path}:{line_number}"
            ) from exc
    return segments


def _render_markdown(source: Path, config: ResolvedConfig, segments: list[Segment]) -> str:
    lines = [
        f"# {source.stem} Transcript",
        "",
        f"- Source: `{source.name}`",
        f"- Model: `{config.values['model']}` via `faster-whisper`",
        f"- Language: `{config.values['language']}`",
        "- Speaker separation: not applied",
        f"- Processing: {config.values['chunk_seconds']}-second chunks",
        "",
        "## Transcript",
        "",
    ]
    lines.extend(
        f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {segment.text}"
        for segment in segments
    )
    return "\n".join(lines) + "\n"


def _render_srt(segments: list[Segment]) -> str:
    blocks = [
        (
            f"{segment.index}\n"
            f"{format_srt_timestamp(segment.start)} --> "
            f"{format_srt_timestamp(segment.end)}\n"
            f"{segment.text}\n"
        )
        for segment in segments
    ]
    return "\n".join(blocks)


def _render_jsonl(segments: list[Segment]) -> str:
    return "".join(
        json.dumps(segment.to_dict(), ensure_ascii=False) + "\n" for segment in segments
    )


def _default_recognizer(model_path: Path, config: ResolvedConfig) -> SpeechRecognizer:
    return FasterWhisperAdapter(
        model_path=model_path,
        language=str(config.values["language"]),
        beam_size=int(config.values["beam_size"]),
        device=str(config.values["device"]),
        compute_type=str(config.values["compute_type"]),
    )


class TranscriptionPipeline:
    def __init__(
        self,
        *,
        config: ResolvedConfig,
        logger: logging.Logger,
        ffmpeg: FfmpegAdapter | None = None,
        recognizer_factory: RecognizerFactory = _default_recognizer,
    ) -> None:
        self.config = config
        self.logger = logger
        self.ffmpeg = ffmpeg or FfmpegAdapter(
            str(config.values["ffmpeg_executable"]),
            int(config.values["subprocess_timeout_seconds"]),
            logger,
        )
        self.recognizer_factory = recognizer_factory

    def transcribe(
        self,
        source: Path,
        *,
        dry_run: bool,
        overwrite: bool,
    ) -> TranscriptionResult:
        source_path = source.expanduser().resolve()
        if not source_path.is_file():
            raise ValidationError(f"Source audio file not found: {source_path}")
        output_dir = self.config.path("output_dir")
        state_dir = self.config.path("state_dir")
        model_dir = self.config.path("model_dir")
        cache_dir = self.config.path("cache_dir")
        if output_dir is None:
            raise ValidationError(
                "output_dir is required. Set it in config or pass --output-dir."
            )
        assert state_dir is not None
        assert model_dir is not None
        assert cache_dir is not None

        source_stat = source_path.stat()
        self.logger.info("Hashing source audio: %s", source_path)
        source_hash = sha256_file(source_path)
        fingerprint = _fingerprint(source_hash, source_stat.st_size, self.config)
        outputs = output_paths(output_dir, source_path)

        if outputs.manifest.exists():
            try:
                existing_manifest = read_json(outputs.manifest)
            except (OSError, ValueError, json.JSONDecodeError):
                existing_manifest = {}
            if existing_manifest.get("fingerprint") == fingerprint:
                validate_artifact(outputs.manifest, verify_source=True)
                segment_count = int(existing_manifest.get("segment_count", 0))
                log_success(self.logger, "Outputs are unchanged and valid")
                return TranscriptionResult(
                    status="unchanged",
                    source=source_path,
                    fingerprint=fingerprint,
                    outputs=outputs,
                    segment_count=segment_count,
                )

        existing_outputs = [path for path in outputs.as_dict().values() if Path(path).exists()]
        if existing_outputs and not overwrite:
            joined = ", ".join(existing_outputs)
            raise ValidationError(
                f"Output already exists and differs or is incomplete: {joined}. "
                "Use --overwrite to replace it."
            )
        status = "replaced" if existing_outputs else "created"
        if dry_run:
            return TranscriptionResult(
                status="planned",
                source=source_path,
                fingerprint=fingerprint,
                outputs=outputs,
                segment_count=0,
            )

        self.ffmpeg.ensure_available()
        model_path = resolve_local_model(str(self.config.values["model"]), model_dir)
        configure_huggingface_cache(cache_dir)

        job_dir = job_directory(state_dir, source_path, fingerprint)
        chunks_dir = job_dir / "chunks"
        normalized = job_dir / "normalized_16k_mono.wav"
        checkpoint_segments = job_dir / "segments.jsonl"
        progress_path = job_dir / "progress.json"
        job_dir.mkdir(parents=True, exist_ok=True)
        source_record = {
            "path": str(source_path),
            "size": source_stat.st_size,
            "sha256": source_hash,
        }
        tracker = JobTracker(
            job_dir=job_dir,
            fingerprint=fingerprint,
            source=source_record,
            settings=dict(self.config.values),
            heartbeat_seconds=int(self.config.values["heartbeat_seconds"]),
            logger=self.logger,
        )
        tracker.start()
        try:
            tracker.set_stage("disk-preflight")
            required_initial = estimated_initial_scratch_bytes(source_stat.st_size)
            free_before = ensure_free_space(
                state_dir, required_initial, stage="audio normalization"
            )
            self.logger.info(
                "Scratch preflight passed: required=%d bytes, available=%d bytes",
                required_initial,
                free_before,
            )

            if not normalized.is_file():
                tracker.set_stage("normalizing")
                self.logger.info("Normalizing audio to mono 16 kHz WAV")
                self.ffmpeg.normalize(source_path, normalized)
            normalized_info = inspect_normalized_wav(normalized)
            ensure_free_space(
                state_dir,
                required_chunk_space_bytes(normalized.stat().st_size),
                stage="chunk creation",
            )

            chunks = sorted(chunks_dir.glob("chunk_*.wav"))
            if not chunks:
                tracker.set_stage("splitting")
                self.logger.info("Splitting normalized audio into chunks")
                chunks = self.ffmpeg.split(
                    normalized, chunks_dir, int(self.config.values["chunk_seconds"])
                )
            chunk_duration_seconds = validate_chunk_coverage(normalized_info, chunks)

            processed_chunks: set[str] = set()
            if progress_path.exists():
                progress = read_json(progress_path)
                raw_processed = progress.get("processed_chunks", [])
                if isinstance(raw_processed, list):
                    processed_chunks = {str(name) for name in raw_processed}
            segments = _load_segments(checkpoint_segments)
            next_index = max((segment.index for segment in segments), default=0) + 1

            recognizer = self.recognizer_factory(model_path, self.config)
            for chunk_position, chunk in enumerate(chunks):
                if chunk.name in processed_chunks:
                    self.logger.info("Resuming: skipping completed chunk %s", chunk.name)
                    continue
                offset = chunk_position * int(self.config.values["chunk_seconds"])
                tracker.set_stage(
                    "transcribing",
                    current_chunk=chunk.name,
                    chunk_position=chunk_position + 1,
                    chunk_count=len(chunks),
                )
                self.logger.info(
                    "Transcribing chunk %d/%d at %s",
                    chunk_position + 1,
                    len(chunks),
                    format_timestamp(offset),
                )
                recognize = partial(
                    recognizer.transcribe_chunk,
                    chunk,
                    offset=float(offset),
                    first_index=next_index,
                )
                new_segments, detected_language, probability = tracker.run_with_heartbeat(
                    recognize
                )
                append_jsonl(
                    checkpoint_segments, [segment.to_dict() for segment in new_segments]
                )
                segments.extend(new_segments)
                next_index += len(new_segments)
                processed_chunks.add(chunk.name)
                atomic_write_json(
                    progress_path,
                    {
                        "schema_version": 1,
                        "processed_chunks": sorted(processed_chunks),
                        "segment_count": len(segments),
                        "detected_language": detected_language,
                        "language_probability": probability,
                    },
                )
                tracker.checkpoint()

            if len(processed_chunks) != len(chunks):
                raise ValidationError("Not all chunks were processed")
            segments.sort(key=lambda item: item.index)

            last_segment_end = max((segment.end for segment in segments), default=0.0)
            if last_segment_end > normalized_info.duration_seconds + 1.0:
                raise ValidationError(
                    "Last transcript segment exceeds decoded audio duration: "
                    f"segment_end={last_segment_end:.3f}s, "
                    f"decoded={normalized_info.duration_seconds:.3f}s"
                )

            if sha256_file(source_path) != source_hash:
                raise ValidationError(
                    "Source audio changed during transcription; outputs were not committed"
                )

            tracker.set_stage("committing")
            output_dir.mkdir(parents=True, exist_ok=True)
            pending_markdown = outputs.markdown.with_name(f".{outputs.markdown.name}.pending")
            pending_srt = outputs.srt.with_name(f".{outputs.srt.name}.pending")
            pending_jsonl = outputs.jsonl.with_name(f".{outputs.jsonl.name}.pending")
            atomic_write_text(
                pending_markdown, _render_markdown(source_path, self.config, segments)
            )
            atomic_write_text(pending_srt, _render_srt(segments))
            atomic_write_text(pending_jsonl, _render_jsonl(segments))
            if "## Transcript" not in pending_markdown.read_text(encoding="utf-8"):
                raise ValidationError("Generated Markdown is invalid")

            for pending, final in (
                (pending_markdown, outputs.markdown),
                (pending_srt, outputs.srt),
                (pending_jsonl, outputs.jsonl),
            ):
                os.replace(pending, final)

            completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            output_records = {
                label: {
                    "path": str(path),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
                for label, path in (
                    ("markdown", outputs.markdown),
                    ("srt", outputs.srt),
                    ("jsonl", outputs.jsonl),
                )
            }
            atomic_write_json(
                outputs.manifest,
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "tool": "tkn-audio-transcriber",
                    "tool_version": __version__,
                    "completed_at": completed_at,
                    "fingerprint": fingerprint,
                    "source": source_record,
                    "decoded_audio": {
                        **normalized_info.to_dict(),
                        "chunk_duration_seconds": chunk_duration_seconds,
                        "last_segment_end_seconds": last_segment_end,
                    },
                    "model": {
                        "requested": self.config.values["model"],
                        "resolved_path": str(model_path),
                    },
                    "settings": {
                        key: self.config.values[key]
                        for key in (
                            "language",
                            "chunk_seconds",
                            "beam_size",
                            "compute_type",
                            "device",
                        )
                    },
                    "segment_count": len(segments),
                    "outputs": output_records,
                },
            )
            validate_artifact(outputs.manifest, verify_source=True)

            if not bool(self.config.values["keep_working_files"]):
                for working_file in [normalized, *chunks]:
                    try:
                        working_file.unlink(missing_ok=True)
                    except OSError as exc:
                        self.logger.warning(
                            "Could not remove working file %s: %s", working_file, exc
                        )
                with suppress(OSError):
                    chunks_dir.rmdir()
            tracker.complete(outputs.manifest)
            log_success(self.logger, "Transcription completed: %s", outputs.manifest)
            return TranscriptionResult(
                status=status,
                source=source_path,
                fingerprint=fingerprint,
                outputs=outputs,
                segment_count=len(segments),
            )
        except BaseException as exc:
            tracker.fail(exc)
            raise

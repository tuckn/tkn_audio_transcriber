from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Protocol

from . import __version__
from .audio_integrity import (
    AZURE_MAX_DURATION_SECONDS,
    AZURE_MAX_FILE_BYTES,
    ensure_free_space,
    estimated_initial_scratch_bytes,
    inspect_normalized_wav,
    required_derived_audio_space_bytes,
    validate_azure_audio_duration,
    validate_azure_upload_limits,
    validate_chunk_coverage,
)
from .azure_speech_adapter import (
    AZURE_AUTHENTICATION_METHOD,
    AzureSpeechFastAdapter,
    AzureTranscription,
    endpoint_type,
)
from .config import (
    AzureSpeechTranscriptionConfig,
    LocalTranscriptionConfig,
    ResolvedConfig,
)
from .errors import CloudUploadApprovalError, ValidationError
from .ffmpeg_adapter import FfmpegAdapter
from .gpu_runtime import validate_cuda_runtime
from .io_utils import (
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    read_json,
    sha256_file,
)
from .job_state import JobTracker
from .logging_config import log_success
from .model_store import ensure_local_model
from .models import Segment, TranscriptionResult
from .paths import job_directory, output_paths
from .validation import MANIFEST_SCHEMA_VERSION, validate_artifact
from .whisper_adapter import FasterWhisperAdapter, SpeechRecognizer

RecognizerFactory = Callable[[Path, LocalTranscriptionConfig], SpeechRecognizer]


class AzureRecognizer(Protocol):
    def transcribe(self, upload_audio: Path) -> AzureTranscription: ...


AzureRecognizerFactory = Callable[
    [AzureSpeechTranscriptionConfig, logging.Logger], AzureRecognizer
]


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


def _fingerprint_settings(config: ResolvedConfig) -> dict[str, object]:
    transcription = config.transcription
    if isinstance(transcription, LocalTranscriptionConfig):
        return {
            "mode": transcription.mode,
            "provider": transcription.provider,
            "model": transcription.model,
            "language": transcription.language,
            "chunk_seconds": transcription.chunk_seconds,
            "beam_size": transcription.beam_size,
            "compute_type": transcription.compute_type,
            "device": transcription.device,
        }
    return {
        "mode": transcription.mode,
        "provider": transcription.provider,
        "azure_speech_endpoint": transcription.endpoint,
        "azure_speech_region": transcription.region,
        "azure_speech_api_version": transcription.api_version,
        "azure_speech_locale": transcription.locale,
        "azure_speech_diarization_enabled": transcription.diarization_enabled,
        "azure_speech_max_speakers": transcription.max_speakers,
        "upload_format": "flac",
    }


def _fingerprint(source_hash: str, source_size: int, config: ResolvedConfig) -> str:
    payload = {
        "source_sha256": source_hash,
        "source_size": source_size,
        "tool_version": __version__,
        "profile": config.profile_selector,
        "settings": _fingerprint_settings(config),
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
                    speaker=(
                        None if value.get("speaker") is None else str(value["speaker"])
                    ),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"Invalid checkpoint JSONL at {path}:{line_number}"
            ) from exc
    return segments


def _bound_local_segments(
    segments: list[Segment],
    chunks: list[Path],
    *,
    chunk_seconds: int,
    decoded_duration: float,
) -> tuple[list[Segment], int, int]:
    chunk_bounds = {
        chunk.name: (
            float(position * chunk_seconds),
            min(
                float(position * chunk_seconds)
                + inspect_normalized_wav(chunk).duration_seconds,
                decoded_duration,
            ),
        )
        for position, chunk in enumerate(chunks)
    }
    bounded: list[Segment] = []
    clipped_count = 0
    dropped_count = 0
    for segment in sorted(segments, key=lambda item: item.index):
        if not math.isfinite(segment.start) or not math.isfinite(segment.end):
            raise ValidationError(
                f"Transcript segment has a non-finite timestamp: index={segment.index}"
            )
        if segment.end <= segment.start:
            raise ValidationError(
                f"Transcript segment has an invalid time range: index={segment.index}, "
                f"start={segment.start:.3f}s, end={segment.end:.3f}s"
            )
        bounds = chunk_bounds.get(segment.chunk)
        if bounds is None:
            raise ValidationError(
                f"Transcript segment references an unknown chunk: {segment.chunk}"
            )
        chunk_start, chunk_end = bounds
        bounded_start = max(segment.start, chunk_start)
        bounded_end = min(segment.end, chunk_end)
        if bounded_end <= bounded_start:
            dropped_count += 1
            continue
        if bounded_start != segment.start or bounded_end != segment.end:
            clipped_count += 1
        bounded.append(
            replace(
                segment,
                index=len(bounded) + 1,
                start=bounded_start,
                end=bounded_end,
            )
        )
    return bounded, clipped_count, dropped_count


def _segment_text(segment: Segment, *, markdown: bool) -> str:
    if segment.speaker is None:
        return segment.text
    if markdown:
        return f"[Speaker {segment.speaker}] {segment.text}"
    return f"Speaker {segment.speaker}: {segment.text}"


def _render_markdown(source: Path, config: ResolvedConfig, segments: list[Segment]) -> str:
    transcription = config.transcription
    if isinstance(transcription, LocalTranscriptionConfig):
        model = transcription.model
        language = transcription.language
        speaker_separation = False
        chunk_seconds: int | None = transcription.chunk_seconds
    else:
        model = transcription.provider
        language = transcription.locale
        speaker_separation = transcription.diarization_enabled
        chunk_seconds = None
    lines = [
        "---",
        f"source: {json.dumps(source.name, ensure_ascii=False)}",
        f"profile: {json.dumps(config.profile_selector, ensure_ascii=False)}",
        f"model: {json.dumps(model, ensure_ascii=False)}",
        f"engine: {json.dumps(transcription.provider)}",
        f"language: {json.dumps(language, ensure_ascii=False)}",
        f"speaker_separation: {str(speaker_separation).lower()}",
        f"chunk_seconds: {json.dumps(chunk_seconds)}",
        'transcriber: "tkn-audio-transcriber"',
        f"transcriber_version: {json.dumps(__version__)}",
        "---",
        "",
        f"# {source.stem} Transcript",
        "",
        "## Transcript",
        "",
    ]
    lines.extend(
        f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] "
        f"{_segment_text(segment, markdown=True)}"
        for segment in segments
    )
    return "\n".join(lines) + "\n"


def _render_srt(segments: list[Segment]) -> str:
    blocks = [
        (
            f"{segment.index}\n"
            f"{format_srt_timestamp(segment.start)} --> "
            f"{format_srt_timestamp(segment.end)}\n"
            f"{_segment_text(segment, markdown=False)}\n"
        )
        for segment in segments
    ]
    return "\n".join(blocks)


def _render_jsonl(segments: list[Segment]) -> str:
    return "".join(
        json.dumps(segment.to_dict(), ensure_ascii=False) + "\n" for segment in segments
    )


def _default_recognizer(
    model_path: Path, config: LocalTranscriptionConfig
) -> SpeechRecognizer:
    return FasterWhisperAdapter(
        model_path=model_path,
        language=config.language,
        beam_size=config.beam_size,
        device=config.device,
        compute_type=config.compute_type,
    )


def _default_azure_recognizer(
    config: AzureSpeechTranscriptionConfig, logger: logging.Logger
) -> AzureRecognizer:
    return AzureSpeechFastAdapter(config=config, logger=logger)


def _dry_run_plan(config: ResolvedConfig) -> dict[str, object]:
    transcription = config.transcription
    if isinstance(transcription, AzureSpeechTranscriptionConfig):
        return {
            "mode": transcription.mode,
            "profile": config.profile_selector,
            "provider": transcription.provider,
            "endpoint_type": endpoint_type(transcription.endpoint),
            "region": transcription.region,
            "api_version": transcription.api_version,
            "locale": transcription.locale,
            "diarization_enabled": transcription.diarization_enabled,
            "upload_format": "flac",
            "authentication_method": AZURE_AUTHENTICATION_METHOD,
            "account_selection_required": True,
            "cloud_upload_approval_required": True,
            "cloud_upload_approved": False,
            "network_calls": 0,
            "normalized_audio_validation": {
                "status": "deferred_until_actual_run",
                "duration_must_be_less_than_seconds": AZURE_MAX_DURATION_SECONDS,
                "size_must_be_less_than_bytes": AZURE_MAX_FILE_BYTES,
                "size_applies_to": "flac_upload",
            },
        }
    return {
        "mode": transcription.mode,
        "profile": config.profile_selector,
        "provider": transcription.provider,
        "endpoint_type": "local",
        "cloud_upload_approval_required": False,
        "network_calls": 0,
    }


def _job_settings(config: ResolvedConfig) -> dict[str, object]:
    settings: dict[str, object] = {
        "mode": config.active_mode,
        "profile": config.profile_selector,
        "output_dir": config.values["output_dir"],
        "state_dir": config.values["state_dir"],
        "ffmpeg_executable": config.values["ffmpeg_executable"],
        "subprocess_timeout_seconds": config.values["subprocess_timeout_seconds"],
        "heartbeat_seconds": config.values["heartbeat_seconds"],
        "keep_working_files": config.values["keep_working_files"],
        **_fingerprint_settings(config),
    }
    if isinstance(config.transcription, AzureSpeechTranscriptionConfig):
        settings["azure_speech_endpoint"] = endpoint_type(config.transcription.endpoint)
    return settings


class TranscriptionPipeline:
    def __init__(
        self,
        *,
        config: ResolvedConfig,
        logger: logging.Logger,
        ffmpeg: FfmpegAdapter | None = None,
        recognizer_factory: RecognizerFactory = _default_recognizer,
        azure_recognizer_factory: AzureRecognizerFactory = _default_azure_recognizer,
    ) -> None:
        self.config = config
        self.logger = logger
        self.ffmpeg = ffmpeg or FfmpegAdapter(
            str(config.values["ffmpeg_executable"]),
            int(config.values["subprocess_timeout_seconds"]),
            logger,
        )
        self.recognizer_factory = recognizer_factory
        self.azure_recognizer_factory = azure_recognizer_factory

    def transcribe(
        self,
        source: Path,
        *,
        dry_run: bool,
        overwrite: bool,
        allow_cloud_upload: bool = False,
    ) -> TranscriptionResult:
        source_path = source.expanduser().resolve()
        if not source_path.is_file():
            raise ValidationError(f"Source media file not found: {source_path}")
        transcription = self.config.transcription
        output_dir = self.config.path("output_dir")
        state_dir = self.config.path("state_dir")
        if output_dir is None:
            raise ValidationError(
                "folders.output is required. Set it in config or pass --output-dir."
            )
        assert state_dir is not None

        if (
            not dry_run
            and isinstance(transcription, LocalTranscriptionConfig)
            and transcription.device.casefold() == "cuda"
        ):
            validate_cuda_runtime(transcription.device)
            self.logger.info("CUDA GPU runtime preflight passed")

        source_stat = source_path.stat()
        self.logger.info("Hashing source media: %s", source_path)
        source_hash = sha256_file(source_path)
        fingerprint = _fingerprint(source_hash, source_stat.st_size, self.config)
        outputs = output_paths(
            output_dir,
            source_path,
            mode=self.config.active_mode,
            profile=self.config.active_profile,
        )

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
                plan=_dry_run_plan(self.config),
            )
        if isinstance(transcription, AzureSpeechTranscriptionConfig) and not allow_cloud_upload:
            raise CloudUploadApprovalError(
                "Azure Speech would upload derived audio. Re-run this command with "
                "--allow-cloud-upload after reviewing the source and provider settings."
            )

        self.ffmpeg.ensure_available()
        model_path: Path | None = None
        if isinstance(transcription, LocalTranscriptionConfig):
            model_dir = self.config.path("model_dir")
            cache_dir = self.config.path("cache_dir")
            assert model_dir is not None and cache_dir is not None
            model_path = ensure_local_model(
                model=transcription.model,
                model_dir=model_dir,
                cache_dir=cache_dir,
                logger=self.logger,
            )

        job_dir = job_directory(state_dir, source_path, fingerprint)
        chunks_dir = job_dir / "chunks"
        normalized = job_dir / "normalized_16k_mono.wav"
        cloud_audio = job_dir / "normalized_16k_mono.flac"
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
            settings=_job_settings(self.config),
            heartbeat_seconds=int(self.config.values["heartbeat_seconds"]),
            logger=self.logger,
        )
        tracker.start()
        try:
            tracker.set_stage("disk-preflight")
            required_initial = estimated_initial_scratch_bytes(source_stat.st_size)
            free_before = ensure_free_space(
                state_dir, required_initial, stage="media audio normalization"
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
            attempts = 0
            retries = 0
            request_id: str | None = None
            authentication_method = "local"
            chunks: list[Path] = []
            chunk_duration_seconds: float | None = None
            if isinstance(transcription, LocalTranscriptionConfig):
                assert model_path is not None
                ensure_free_space(
                    state_dir,
                    required_derived_audio_space_bytes(normalized.stat().st_size),
                    stage="chunk creation",
                )
                chunks = sorted(chunks_dir.glob("chunk_*.wav"))
                if not chunks:
                    tracker.set_stage("splitting")
                    self.logger.info("Splitting normalized audio into chunks")
                    chunks = self.ffmpeg.split(
                        normalized, chunks_dir, transcription.chunk_seconds
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

                recognizer = self.recognizer_factory(model_path, transcription)
                for chunk_position, chunk in enumerate(chunks):
                    if chunk.name in processed_chunks:
                        self.logger.info("Resuming: skipping completed chunk %s", chunk.name)
                        continue
                    offset = chunk_position * transcription.chunk_seconds
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
                attempts = len(chunks)
            else:
                validate_azure_audio_duration(normalized_info)
                authentication_method = AZURE_AUTHENTICATION_METHOD
                progress = read_json(progress_path) if progress_path.exists() else {}
                completed_request = progress.get("azure_request_completed") is True
                segments = _load_segments(checkpoint_segments) if completed_request else []
                if completed_request and checkpoint_segments.exists():
                    attempts = int(progress.get("attempts", 1))
                    retries = int(progress.get("retries", max(0, attempts - 1)))
                    raw_request_id = progress.get("request_id")
                    request_id = raw_request_id if isinstance(raw_request_id, str) else None
                    recorded_auth = progress.get("authentication_method")
                    # Old checkpoints did not record authentication. Do not attribute
                    # an earlier submission to today's browser-only implementation.
                    authentication_method = (
                        recorded_auth
                        if isinstance(recorded_auth, str)
                        and recorded_auth in (AZURE_AUTHENTICATION_METHOD, "DefaultAzureCredential")
                        else "unknown"
                    )
                    self.logger.info("Resuming from completed Azure Speech response checkpoint")
                else:
                    ensure_free_space(
                        state_dir,
                        required_derived_audio_space_bytes(normalized.stat().st_size),
                        stage="FLAC encoding",
                    )
                    tracker.set_stage("encoding-flac")
                    self.logger.info("Losslessly encoding normalized audio to FLAC")
                    # Always regenerate: an interrupted encoding may leave a partial file.
                    tracker.run_with_heartbeat(
                        partial(self.ffmpeg.encode_flac, normalized, cloud_audio)
                    )
                    validate_azure_upload_limits(cloud_audio, normalized_info)
                    self.logger.info(
                        "FLAC upload ready: wav_bytes=%d, flac_bytes=%d",
                        normalized.stat().st_size,
                        cloud_audio.stat().st_size,
                    )
                    tracker.set_stage(
                        "transcribing",
                        current_chunk=cloud_audio.name,
                        chunk_position=1,
                        chunk_count=1,
                    )
                    azure_recognizer = self.azure_recognizer_factory(
                        transcription, self.logger
                    )
                    azure_result = tracker.run_with_heartbeat(
                        partial(azure_recognizer.transcribe, cloud_audio)
                    )
                    segments = azure_result.segments
                    attempts = azure_result.attempts
                    retries = azure_result.retries
                    request_id = azure_result.request_id
                    atomic_write_text(checkpoint_segments, _render_jsonl(segments))
                    atomic_write_json(
                        progress_path,
                        {
                            "schema_version": 1,
                            "azure_request_completed": True,
                            "authentication_method": authentication_method,
                            "segment_count": len(segments),
                            "attempts": attempts,
                            "retries": retries,
                            "request_id": request_id,
                        },
                    )
                    tracker.checkpoint()
            timestamp_clipped_segments = 0
            timestamp_dropped_segments = 0
            if isinstance(transcription, LocalTranscriptionConfig):
                (
                    segments,
                    timestamp_clipped_segments,
                    timestamp_dropped_segments,
                ) = _bound_local_segments(
                    segments,
                    chunks,
                    chunk_seconds=transcription.chunk_seconds,
                    decoded_duration=normalized_info.duration_seconds,
                )
                if timestamp_clipped_segments or timestamp_dropped_segments:
                    self.logger.warning(
                        "Adjusted transcript segments outside decoded chunk boundaries: "
                        "clipped=%d, dropped=%d",
                        timestamp_clipped_segments,
                        timestamp_dropped_segments,
                    )
            else:
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
                    "Source media changed during transcription; outputs were not committed"
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
            decoded_audio: dict[str, object] = {
                **normalized_info.to_dict(),
                "last_segment_end_seconds": last_segment_end,
                "timestamp_adjustments": {
                    "clipped_segments": timestamp_clipped_segments,
                    "dropped_segments": timestamp_dropped_segments,
                },
            }
            if chunk_duration_seconds is not None:
                decoded_audio["chunk_duration_seconds"] = chunk_duration_seconds
            if isinstance(transcription, LocalTranscriptionConfig):
                settings: dict[str, object] = {
                    "language": transcription.language,
                    "chunk_seconds": transcription.chunk_seconds,
                    "beam_size": transcription.beam_size,
                    "compute_type": transcription.compute_type,
                    "device": transcription.device,
                }
                api_version: str | None = None
                region: str | None = None
                locale = transcription.language
                diarization_enabled = False
                model_requested: str | None = transcription.model
            else:
                settings = {
                    "endpoint_type": endpoint_type(transcription.endpoint),
                    "region": transcription.region,
                    "api_version": transcription.api_version,
                    "locale": transcription.locale,
                    "diarization_enabled": transcription.diarization_enabled,
                    "max_speakers": transcription.max_speakers,
                    "timeout_seconds": transcription.timeout_seconds,
                    "max_retries": transcription.max_retries,
                    "upload_format": "flac",
                }
                api_version = transcription.api_version
                region = transcription.region
                locale = transcription.locale
                diarization_enabled = transcription.diarization_enabled
                model_requested = None
            provenance: dict[str, object] = {
                "mode": transcription.mode,
                "profile": self.config.profile_selector,
                "provider": transcription.provider,
                "api_version": api_version,
                "region": region,
                "locale": locale,
                "diarization_enabled": diarization_enabled,
                "authentication_method": authentication_method,
                "source_sha256": source_hash,
                "decoded_duration_seconds": normalized_info.duration_seconds,
                "attempts": attempts,
                "retries": retries,
                "request_id": request_id,
                "tool_version": __version__,
            }
            atomic_write_json(
                outputs.manifest,
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "tool": "tkn-audio-transcriber",
                    "tool_version": __version__,
                    "completed_at": completed_at,
                    "fingerprint": fingerprint,
                    "provider": transcription.provider,
                    "provenance": provenance,
                    "source": source_record,
                    "decoded_audio": decoded_audio,
                    "model": {
                        "requested": model_requested,
                        "resolved_path": str(model_path) if model_path is not None else None,
                    },
                    "settings": settings,
                    "segment_count": len(segments),
                    "outputs": output_records,
                },
            )
            validate_artifact(outputs.manifest, verify_source=True)

            if not bool(self.config.values["keep_working_files"]):
                for working_file in [normalized, cloud_audio, *chunks]:
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

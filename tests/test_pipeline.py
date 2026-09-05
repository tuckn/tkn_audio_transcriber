from __future__ import annotations

import json
import logging
import wave
from pathlib import Path

import pytest
import yaml

from audio_transcriber import __version__
from audio_transcriber.audio_integrity import WavInfo
from audio_transcriber.azure_speech_adapter import AzureTranscription
from audio_transcriber.config import (
    AzureSpeechTranscriptionConfig,
    LocalTranscriptionConfig,
    ResolvedConfig,
    resolve_config,
)
from audio_transcriber.errors import (
    AzureSpeechError,
    CloudUploadApprovalError,
    ExternalProcessError,
    ValidationError,
)
from audio_transcriber.io_utils import sha256_file
from audio_transcriber.models import Segment
from audio_transcriber.pipeline import TranscriptionPipeline
from audio_transcriber.validation import validate_artifact


class FakeFfmpeg:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def ensure_available(self) -> None:
        self.calls.append("ensure")

    def normalize(self, source: Path, destination: Path) -> None:
        self.calls.append("normalize")
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(destination, seconds=20)

    def split(self, normalized: Path, chunk_dir: Path, chunk_seconds: int) -> list[Path]:
        self.calls.append("split")
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunks = [chunk_dir / "chunk_000000.wav", chunk_dir / "chunk_000001.wav"]
        for chunk in chunks:
            _write_wav(chunk, seconds=10)
        return chunks

    def encode_flac(self, normalized: Path, destination: Path) -> None:
        self.calls.append("encode-flac")
        assert normalized.read_bytes().startswith(b"RIFF")
        destination.write_bytes(b"fLaC" + b"synthetic compressed audio")


def _write_wav(path: Path, *, seconds: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 16_000 * seconds)


class FakeRecognizer:
    def __init__(self, calls: list[str], fail_on: str | None = None) -> None:
        self.calls = calls
        self.fail_on = fail_on

    def transcribe_chunk(
        self, chunk: Path, *, offset: float, first_index: int
    ) -> tuple[list[Segment], str, float]:
        self.calls.append(chunk.name)
        if chunk.name == self.fail_on:
            raise RuntimeError("simulated interruption")
        return (
            [
                Segment(
                    index=first_index,
                    start=offset + 0.1,
                    end=offset + 1.2,
                    text=f"text from {chunk.name}",
                    chunk=chunk.name,
                )
            ],
            "ja",
            0.99,
        )


class OutOfBoundsRecognizer:
    def transcribe_chunk(
        self, chunk: Path, *, offset: float, first_index: int
    ) -> tuple[list[Segment], str, float]:
        if chunk.name == "chunk_000000.wav":
            segments = [
                Segment(
                    index=first_index,
                    start=offset + 9.5,
                    end=offset + 12.0,
                    text="clipped first chunk",
                    chunk=chunk.name,
                )
            ]
        else:
            segments = [
                Segment(
                    index=first_index,
                    start=offset + 8.0,
                    end=offset + 15.0,
                    text="clipped final chunk",
                    chunk=chunk.name,
                ),
                Segment(
                    index=first_index + 1,
                    start=offset + 11.0,
                    end=offset + 15.0,
                    text="outside decoded audio",
                    chunk=chunk.name,
                ),
            ]
        return segments, "ja", 0.99


class FakeAzureRecognizer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def transcribe(self, upload_audio: Path) -> AzureTranscription:
        assert upload_audio.suffix == ".flac"
        assert upload_audio.read_bytes().startswith(b"fLaC")
        self.calls.append(upload_audio.name)
        return AzureTranscription(
            segments=[
                Segment(
                    index=1,
                    start=0.1,
                    end=1.2,
                    text="Azure text",
                    chunk="azure-whole-file",
                    speaker="1",
                )
            ],
            attempts=2,
            retries=1,
            request_id="request-123",
        )


def make_config(tmp_path: Path, *, profile: str | None = None) -> ResolvedConfig:
    model_dir = tmp_path / "models"
    model = model_dir / "faster-whisper-small"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    return resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        profile=profile,
        cli_overrides={
            "output_dir": str(tmp_path / "outputs"),
            "state_dir": str(tmp_path / "state"),
            "model_dir": str(model_dir),
            "cache_dir": str(tmp_path / "cache"),
            "model": "small",
            "chunk_seconds": 10,
        },
    )


def make_azure_config(tmp_path: Path) -> ResolvedConfig:
    return resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        profile="cloud/azure-ja",
        cli_overrides={
            "azure_speech_endpoint": "https://example.cognitiveservices.azure.com/",
            "azure_speech_diarization_enabled": True,
            "output_dir": str(tmp_path / "outputs"),
            "state_dir": str(tmp_path / "state"),
        },
    )


def make_logger() -> logging.Logger:
    logger = logging.getLogger("audio_transcriber.tests")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def test_dry_run_changes_nothing_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "meeting.flac"
    source.write_bytes(b"immutable source")
    before = sha256_file(source)
    calls: list[str] = []
    pipeline = TranscriptionPipeline(
        config=make_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(calls),  # type: ignore[arg-type]
    )
    result = pipeline.transcribe(source, dry_run=True, overwrite=False)
    assert result.status == "planned"
    assert calls == []
    assert sha256_file(source) == before
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "state").exists()


def test_dry_run_accepts_mp4_source(tmp_path: Path) -> None:
    source = tmp_path / "town-hall.mp4"
    source.write_bytes(b"immutable video container")
    pipeline = TranscriptionPipeline(
        config=make_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
    )

    result = pipeline.transcribe(source, dry_run=True, overwrite=False)

    assert result.status == "planned"
    assert result.source == source.resolve()
    assert result.outputs.markdown.name == "town-hall__local__local-small_transcript.md"


def test_transcribe_ensures_missing_model_before_audio_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "meeting.flac"
    source.write_bytes(b"immutable source")
    config = make_config(tmp_path)
    target = tmp_path / "models" / "faster-whisper-small"
    (target / "model.bin").unlink()
    (target / "config.json").unlink()
    target.rmdir()
    ensure_calls: list[tuple[str, Path, Path]] = []

    def fake_ensure_local_model(
        *,
        model: str,
        model_dir: Path,
        cache_dir: Path,
        logger: logging.Logger,
    ) -> Path:
        ensure_calls.append((model, model_dir, cache_dir))
        target.mkdir(parents=True)
        (target / "model.bin").write_bytes(b"model")
        (target / "config.json").write_text("{}", encoding="utf-8")
        return target

    monkeypatch.setattr(
        "audio_transcriber.pipeline.ensure_local_model", fake_ensure_local_model
    )
    pipeline = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    )

    result = pipeline.transcribe(source, dry_run=False, overwrite=False)

    assert result.status == "created"
    assert ensure_calls == [
        ("small", tmp_path / "models", tmp_path / "cache"),
    ]


def test_cuda_runtime_is_checked_before_hashing_model_or_audio_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "meeting.flac"
    source.write_bytes(b"immutable source")
    calls: list[str] = []

    def reject_cuda(device: str) -> None:
        calls.append(f"cuda:{device}")
        raise ValidationError("CUDA GPU runtime preflight failed before audio processing")

    def reject_hash(path: Path) -> str:
        raise AssertionError(f"source must not be hashed after failed preflight: {path}")

    def reject_model(**kwargs: object) -> Path:
        raise AssertionError(f"model must not be resolved after failed preflight: {kwargs}")

    monkeypatch.setattr("audio_transcriber.pipeline.validate_cuda_runtime", reject_cuda)
    monkeypatch.setattr("audio_transcriber.pipeline.sha256_file", reject_hash)
    monkeypatch.setattr("audio_transcriber.pipeline.ensure_local_model", reject_model)
    ffmpeg_calls: list[str] = []
    pipeline = TranscriptionPipeline(
        config=make_config(tmp_path, profile="local/gpu-quality"),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
    )

    with pytest.raises(ValidationError, match="CUDA GPU runtime preflight failed"):
        pipeline.transcribe(source, dry_run=False, overwrite=False)

    assert calls == ["cuda:cuda"]
    assert ffmpeg_calls == []
    assert not (tmp_path / "state").exists()


def test_end_to_end_commit_validate_unchanged_and_source_immutability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "会議.flac"
    source.write_bytes(b"immutable source")
    before = sha256_file(source)
    ffmpeg_calls: list[str] = []
    recognition_calls: list[str] = []
    azure_factory_calls: list[str] = []
    config = make_config(tmp_path)

    def azure_factory(
        config: AzureSpeechTranscriptionConfig, logger: logging.Logger
    ) -> FakeAzureRecognizer:
        azure_factory_calls.append("created")
        return FakeAzureRecognizer([])

    pipeline = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer(recognition_calls),
        azure_recognizer_factory=azure_factory,
    )
    result = pipeline.transcribe(source, dry_run=False, overwrite=False)
    assert result.status == "created"
    assert result.segment_count == 2
    assert sha256_file(source) == before
    assert result.outputs.markdown.is_file()
    assert result.outputs.srt.is_file()
    assert result.outputs.jsonl.is_file()
    assert result.outputs.manifest.is_file()
    assert validate_artifact(result.outputs.manifest, verify_source=True)["status"] == "valid"
    markdown = result.outputs.markdown.read_text(encoding="utf-8")
    _, frontmatter, body = markdown.split("---", maxsplit=2)
    assert yaml.safe_load(frontmatter) == {
        "source": "会議.flac",
        "profile": "local/local-small",
        "model": "small",
        "engine": "faster-whisper",
        "language": "ja",
        "speaker_separation": False,
        "chunk_seconds": 10,
        "transcriber": "tkn-audio-transcriber",
        "transcriber_version": __version__,
    }
    assert body.startswith("\n\n# 会議 Transcript\n\n## Transcript\n\n")
    assert "- Source:" not in markdown
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["provenance"]["profile"] == "local/local-small"
    assert manifest["decoded_audio"]["duration_seconds"] == 20.0
    legacy_manifest = dict(manifest)
    legacy_manifest["schema_version"] = 2
    legacy_manifest["provenance"] = dict(manifest["provenance"])
    legacy_manifest["provenance"].pop("profile")
    legacy_manifest_path = result.outputs.manifest.with_name("legacy-schema-2.manifest.json")
    legacy_manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    assert validate_artifact(legacy_manifest_path, verify_source=True)["status"] == "valid"
    local_jsonl = json.loads(result.outputs.jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert "speaker" not in local_jsonl
    assert azure_factory_calls == []
    job_files = list((tmp_path / "state" / "jobs").glob("*/job.json"))
    assert json.loads(job_files[0].read_text(encoding="utf-8"))["status"] == "completed"

    second = pipeline.transcribe(source, dry_run=False, overwrite=False)
    assert second.status == "unchanged"
    assert recognition_calls == ["chunk_000000.wav", "chunk_000001.wav"]


def test_resume_skips_completed_chunks(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    config = make_config(tmp_path)
    first_calls: list[str] = []
    first = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer(
            first_calls, fail_on="chunk_000001.wav"
        ),
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        first.transcribe(source, dry_run=False, overwrite=False)
    assert first_calls == ["chunk_000000.wav", "chunk_000001.wav"]
    failed_job = next((tmp_path / "state" / "jobs").glob("*/job.json"))
    assert json.loads(failed_job.read_text(encoding="utf-8"))["status"] == "failed"

    resumed_calls: list[str] = []
    resumed = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer(resumed_calls),
    )
    result = resumed.transcribe(source, dry_run=False, overwrite=False)
    assert result.status == "created"
    assert resumed_calls == ["chunk_000001.wav"]
    assert result.segment_count == 2


def test_local_segments_outside_chunk_boundaries_are_adjusted(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    pipeline = TranscriptionPipeline(
        config=make_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: OutOfBoundsRecognizer(),
    )

    result = pipeline.transcribe(source, dry_run=False, overwrite=False)

    assert result.status == "created"
    assert result.segment_count == 2
    records = [
        json.loads(line)
        for line in result.outputs.jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert [(record["index"], record["start"], record["end"]) for record in records] == [
        (1, 9.5, 10.0),
        (2, 18.0, 20.0),
    ]
    assert all(record["text"] != "outside decoded audio" for record in records)
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    assert manifest["decoded_audio"]["last_segment_end_seconds"] == 20.0
    assert manifest["decoded_audio"]["timestamp_adjustments"] == {
        "clipped_segments": 2,
        "dropped_segments": 1,
    }


def test_different_existing_output_requires_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source one")
    config = make_config(tmp_path)
    make = lambda: TranscriptionPipeline(  # noqa: E731
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    )
    make().transcribe(source, dry_run=False, overwrite=False)
    source.write_bytes(b"source two")
    with pytest.raises(ValidationError, match="--overwrite"):
        make().transcribe(source, dry_run=False, overwrite=False)
    replaced = make().transcribe(source, dry_run=False, overwrite=True)
    assert replaced.status == "replaced"


def test_different_profiles_create_distinct_outputs_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")

    first = TranscriptionPipeline(
        config=make_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    ).transcribe(source, dry_run=False, overwrite=False)
    second = TranscriptionPipeline(
        config=make_config(tmp_path, profile="local/local-large"),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    ).transcribe(source, dry_run=False, overwrite=False)

    assert first.status == "created"
    assert second.status == "created"
    assert first.fingerprint != second.fingerprint
    assert first.outputs.markdown.name == "meeting__local__local-small_transcript.md"
    assert second.outputs.markdown.name == "meeting__local__local-large_transcript.md"
    assert first.outputs.markdown.is_file()
    assert second.outputs.markdown.is_file()
    first_manifest = json.loads(first.outputs.manifest.read_text(encoding="utf-8"))
    second_manifest = json.loads(second.outputs.manifest.read_text(encoding="utf-8"))
    assert first_manifest["provenance"]["profile"] == "local/local-small"
    assert second_manifest["provenance"]["profile"] == "local/local-large"


def test_azure_dry_run_never_creates_clients_or_writes(tmp_path: Path) -> None:
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"video container")
    ffmpeg_calls: list[str] = []
    azure_factory_calls: list[str] = []

    def azure_factory(
        config: AzureSpeechTranscriptionConfig, logger: logging.Logger
    ) -> FakeAzureRecognizer:
        azure_factory_calls.append("created")
        return FakeAzureRecognizer([])

    result = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        azure_recognizer_factory=azure_factory,
    ).transcribe(source, dry_run=True, overwrite=False)

    assert result.status == "planned"
    assert result.plan == {
        "mode": "cloud",
        "profile": "cloud/azure-ja",
        "provider": "azure-speech-fast",
        "endpoint_type": "azure-custom-subdomain",
        "region": "japaneast",
        "api_version": "2025-10-15",
        "locale": "ja-JP",
        "diarization_enabled": True,
        "upload_format": "flac",
        "authentication_method": "InteractiveBrowserCredential",
        "account_selection_required": False,
        "authentication_interaction": "if_required",
        "profanity_filter_mode": "None",
        "phrase_list": {"phrases": []},
        "cloud_upload_approval_required": True,
        "cloud_upload_approved": False,
        "network_calls": 0,
        "normalized_audio_validation": {
            "status": "deferred_until_actual_run",
            "duration_must_be_less_than_seconds": 7200,
            "size_must_be_less_than_bytes": 250_000_000,
            "size_applies_to": "flac_upload",
        },
    }
    assert ffmpeg_calls == []
    assert azure_factory_calls == []
    assert not (tmp_path / "outputs").exists()
    assert not (tmp_path / "state").exists()


def test_azure_actual_run_requires_per_run_cloud_approval_before_clients(
    tmp_path: Path,
) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    ffmpeg_calls: list[str] = []
    azure_factory_calls: list[str] = []

    def azure_factory(
        config: AzureSpeechTranscriptionConfig, logger: logging.Logger
    ) -> FakeAzureRecognizer:
        azure_factory_calls.append("created")
        return FakeAzureRecognizer([])

    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        azure_recognizer_factory=azure_factory,
    )

    with pytest.raises(CloudUploadApprovalError, match="--allow-cloud-upload"):
        pipeline.transcribe(source, dry_run=False, overwrite=False)

    assert ffmpeg_calls == []
    assert azure_factory_calls == []
    assert not (tmp_path / "state").exists()


def test_azure_whole_file_flow_preserves_speakers_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "town-hall.mp4"
    source.write_bytes(b"immutable video source")
    ffmpeg_calls: list[str] = []
    azure_calls: list[str] = []

    def reject_local_model(**kwargs: object) -> Path:
        raise AssertionError("Azure provider must not resolve or download a local model")

    monkeypatch.setattr("audio_transcriber.pipeline.ensure_local_model", reject_local_model)
    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        azure_recognizer_factory=lambda config, logger: FakeAzureRecognizer(azure_calls),
    )

    result = pipeline.transcribe(
        source,
        dry_run=False,
        overwrite=False,
        allow_cloud_upload=True,
    )

    assert result.status == "created"
    assert ffmpeg_calls == ["ensure", "normalize", "encode-flac"]
    assert azure_calls == ["normalized_16k_mono.flac"]
    markdown = result.outputs.markdown.read_text(encoding="utf-8")
    assert 'engine: "azure-speech-fast"' in markdown
    assert "[Speaker 1] Azure text" in markdown
    assert "Speaker 1: Azure text" in result.outputs.srt.read_text(encoding="utf-8")
    jsonl = json.loads(result.outputs.jsonl.read_text(encoding="utf-8"))
    assert jsonl["speaker"] == "1"
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["provider"] == "azure-speech-fast"
    assert manifest["provenance"] == {
        "mode": "cloud",
        "profile": "cloud/azure-ja",
        "provider": "azure-speech-fast",
        "api_version": "2025-10-15",
        "region": "japaneast",
        "locale": "ja-JP",
        "diarization_enabled": True,
        "authentication_method": "InteractiveBrowserCredential",
        "source_sha256": sha256_file(source),
        "decoded_duration_seconds": 20.0,
        "attempts": 2,
        "retries": 1,
        "request_id": "request-123",
        "tool_version": __version__,
    }
    assert manifest["model"] == {"requested": None, "resolved_path": None}
    assert manifest["settings"]["endpoint_type"] == "azure-custom-subdomain"
    assert manifest["settings"]["upload_format"] == "flac"
    job = json.loads(next((tmp_path / "state" / "jobs").glob("*/job.json")).read_text())
    assert job["settings"]["azure_speech_endpoint"] == "azure-custom-subdomain"
    assert job["settings"]["upload_format"] == "flac"
    assert not list((tmp_path / "state" / "jobs").glob("*/*.flac"))
    assert not list((tmp_path / "state" / "jobs").glob("*/*.wav"))


def test_azure_duration_limit_is_checked_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    factory_calls: list[str] = []
    monkeypatch.setattr(
        "audio_transcriber.pipeline.inspect_normalized_wav",
        lambda path: WavInfo(
            duration_seconds=7200.0,
            frame_count=1,
            sample_rate=16_000,
            channels=1,
            sample_width=2,
        ),
    )

    def azure_factory(
        config: AzureSpeechTranscriptionConfig, logger: logging.Logger
    ) -> FakeAzureRecognizer:
        factory_calls.append("created")
        return FakeAzureRecognizer([])

    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        azure_recognizer_factory=azure_factory,
    )

    with pytest.raises(ValidationError, match="shorter than 2 hours"):
        pipeline.transcribe(
            source,
            dry_run=False,
            overwrite=False,
            allow_cloud_upload=True,
        )

    assert factory_calls == []
    normalized = next((tmp_path / "state" / "jobs").glob("*/normalized_16k_mono.wav"))
    assert normalized.is_file()
    assert not normalized.with_suffix(".flac").exists()


def test_azure_size_limit_is_checked_before_client_creation(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    factory_calls: list[str] = []

    class OversizeFfmpeg(FakeFfmpeg):
        def encode_flac(self, normalized: Path, destination: Path) -> None:
            super().encode_flac(normalized, destination)
            with destination.open("r+b") as stream:
                stream.seek(250_000_000 - 1)
                stream.write(b"\0")

    def azure_factory(
        config: AzureSpeechTranscriptionConfig, logger: logging.Logger
    ) -> FakeAzureRecognizer:
        factory_calls.append("created")
        return FakeAzureRecognizer([])

    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=OversizeFfmpeg([]),  # type: ignore[arg-type]
        azure_recognizer_factory=azure_factory,
    )

    with pytest.raises(ValidationError, match="smaller than 250 MB"):
        pipeline.transcribe(
            source,
            dry_run=False,
            overwrite=False,
            allow_cloud_upload=True,
        )

    assert factory_calls == []


def test_azure_size_limit_applies_to_flac_and_keep_working_files_preserves_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"source")
    config = make_azure_config(tmp_path)
    config.values["keep_working_files"] = True
    monkeypatch.setattr("audio_transcriber.audio_integrity.AZURE_MAX_FILE_BYTES", 100)
    result = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        azure_recognizer_factory=lambda config, logger: FakeAzureRecognizer([]),
    ).transcribe(source, dry_run=False, overwrite=False, allow_cloud_upload=True)

    assert result.status == "created"
    normalized = next((tmp_path / "state" / "jobs").glob("*/*.wav"))
    compressed = normalized.with_suffix(".flac")
    assert normalized.stat().st_size > 100
    assert 0 < compressed.stat().st_size < 100


def test_azure_encoding_failure_stops_before_clients_and_partial_flac_is_regenerated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"source")
    factory_calls: list[str] = []

    class InterruptedFfmpeg(FakeFfmpeg):
        def encode_flac(self, normalized: Path, destination: Path) -> None:
            destination.write_bytes(b"partial FLAC")
            raise ExternalProcessError("interrupted FLAC encoding")

    def azure_factory(
        config: AzureSpeechTranscriptionConfig, logger: logging.Logger
    ) -> FakeAzureRecognizer:
        factory_calls.append("created")
        return FakeAzureRecognizer([])

    config = make_azure_config(tmp_path)
    with pytest.raises(ExternalProcessError, match="interrupted FLAC"):
        TranscriptionPipeline(
            config=config,
            logger=make_logger(),
            ffmpeg=InterruptedFfmpeg([]),  # type: ignore[arg-type]
            azure_recognizer_factory=azure_factory,
        ).transcribe(source, dry_run=False, overwrite=False, allow_cloud_upload=True)

    assert factory_calls == []
    compressed = next((tmp_path / "state" / "jobs").glob("*/*.flac"))
    assert compressed.read_bytes() == b"partial FLAC"
    ffmpeg_calls: list[str] = []
    result = TranscriptionPipeline(
        config=config,
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        azure_recognizer_factory=azure_factory,
    ).transcribe(source, dry_run=False, overwrite=False, allow_cloud_upload=True)

    assert result.status == "created"
    assert ffmpeg_calls == ["ensure", "encode-flac"]
    assert factory_calls == ["created"]


@pytest.mark.parametrize(
    ("recorded_method", "expected_method"),
    [
        ("InteractiveBrowserCredential", "InteractiveBrowserCredential"),
        ("DefaultAzureCredential", "DefaultAzureCredential"),
        (None, "unknown"),
        ("CANARY_UNEXPECTED_AUTH_VALUE", "unknown"),
    ],
)
def test_azure_completed_checkpoint_skips_flac_encoding_and_resubmission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_method: str | None,
    expected_method: str,
) -> None:
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"source")
    ffmpeg_calls: list[str] = []
    azure_calls: list[str] = []
    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg(ffmpeg_calls),  # type: ignore[arg-type]
        azure_recognizer_factory=lambda config, logger: FakeAzureRecognizer(azure_calls),
    )

    def interrupted_render(*args: object, **kwargs: object) -> str:
        raise RuntimeError("interrupted after Azure response")

    with monkeypatch.context() as patch:
        patch.setattr("audio_transcriber.pipeline._render_markdown", interrupted_render)
        with pytest.raises(RuntimeError, match="interrupted after Azure"):
            pipeline.transcribe(source, dry_run=False, overwrite=False, allow_cloud_upload=True)

    progress_path = next((tmp_path / "state" / "jobs").glob("*/progress.json"))
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["authentication_method"] == "InteractiveBrowserCredential"
    if recorded_method is None:
        del progress["authentication_method"]
    else:
        progress["authentication_method"] = recorded_method
    progress_path.write_text(json.dumps(progress), encoding="utf-8")

    result = pipeline.transcribe(
        source, dry_run=False, overwrite=False, allow_cloud_upload=True
    )
    assert result.status == "created"
    assert ffmpeg_calls.count("encode-flac") == 1
    assert azure_calls == ["normalized_16k_mono.flac"]
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    assert manifest["provenance"]["authentication_method"] == expected_method


def test_provider_changes_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    local = TranscriptionPipeline(
        config=make_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
    ).transcribe(source, dry_run=True, overwrite=False)
    azure = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
    ).transcribe(source, dry_run=True, overwrite=False)

    assert local.fingerprint != azure.fingerprint


def test_azure_failure_never_falls_back_to_local(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    local_factory_calls: list[str] = []

    class FailingAzureRecognizer:
        def transcribe(self, normalized_audio: Path) -> AzureTranscription:
            raise AzureSpeechError("safe Azure failure")

    def local_factory(model: Path, config: LocalTranscriptionConfig) -> FakeRecognizer:
        local_factory_calls.append("created")
        return FakeRecognizer([])

    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path),
        logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=local_factory,
        azure_recognizer_factory=lambda config, logger: FailingAzureRecognizer(),
    )

    with pytest.raises(AzureSpeechError, match="safe Azure failure"):
        pipeline.transcribe(
            source,
            dry_run=False,
            overwrite=False,
            allow_cloud_upload=True,
        )

    assert local_factory_calls == []
    assert not (tmp_path / "outputs").exists()

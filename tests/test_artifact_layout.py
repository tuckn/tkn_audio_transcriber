from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_pipeline import (
    FakeAzureRecognizer,
    FakeFfmpeg,
    FakeRecognizer,
    _write_wav,
    make_azure_config,
    make_config,
    make_logger,
)

from audio_transcriber.errors import ValidationError
from audio_transcriber.ffmpeg_adapter import FfmpegAdapter
from audio_transcriber.io_utils import atomic_copy_file, sha256_file
from audio_transcriber.maintenance import cleanup_jobs
from audio_transcriber.models import TranscriptionResult
from audio_transcriber.paths import output_paths
from audio_transcriber.pipeline import TranscriptionPipeline
from audio_transcriber.validation import validate_artifact


def run(
    root: Path, source: Path, *, profile: str | None = None, overwrite: bool = False,
) -> TranscriptionResult:
    return TranscriptionPipeline(
        config=make_config(root, profile=profile), logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    ).transcribe(source, dry_run=False, overwrite=overwrite)


def test_unicode_full_audio_retained_after_cleanup_and_folder_move(tmp_path: Path) -> None:
    source = tmp_path / "会議 その1.mp4"
    source.write_bytes(b"source")
    result = run(tmp_path, source)
    assert result.outputs.audio.parent.name == f"会議 その1_{sha256_file(source)[:12]}"
    assert result.outputs.audio.name == "audio.flac"
    assert len(list(result.outputs.audio.parent.iterdir())) == 5
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    assert manifest["outputs"]["audio"] == {
        "path": "audio.flac", "size": result.outputs.audio.stat().st_size,
        "sha256": sha256_file(result.outputs.audio),
    }
    assert manifest["audio_conversion"]["source_audio_stream"] == "0:a:0"
    assert manifest["audio_conversion"]["silence_removed"] is False
    assert all(not Path(record["path"]).is_absolute() for record in manifest["outputs"].values())
    assert not list((tmp_path / "state/jobs").glob("*/*.flac"))
    assert cleanup_jobs(tmp_path / "state", older_than_days=0, apply=True)["deleted_count"] == 1
    assert result.outputs.audio.is_file()
    moved = tmp_path / "moved"
    shutil.move(str(result.outputs.audio.parent), moved)
    source.unlink()
    assert validate_artifact(moved / result.outputs.manifest.name, verify_source=False)[
        "status"
    ] == "valid"


def test_same_name_different_sources_and_relocated_source(tmp_path: Path) -> None:
    first_source = tmp_path / "one/会議.mp4"
    other_source = tmp_path / "two/会議.mp4"
    first_source.parent.mkdir()
    other_source.parent.mkdir()
    first_source.write_bytes(b"one")
    other_source.write_bytes(b"two")
    first = run(tmp_path, first_source)
    second = run(tmp_path, other_source)
    assert first.outputs.audio != second.outputs.audio
    relocated = tmp_path / "relocated/会議.mp4"
    relocated.parent.mkdir()
    first_source.rename(relocated)
    assert run(tmp_path, relocated).status == "unchanged"


def test_profile_change_preserves_shared_audio_and_edited_transcript(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    first = run(tmp_path, source)
    audio_bytes = first.outputs.audio.read_bytes()
    audio_mtime = first.outputs.audio.stat().st_mtime_ns
    first.outputs.markdown.write_text("Human corrections", encoding="utf-8")
    second = run(tmp_path, source, profile="local/local-large")
    assert second.status == "created"
    assert second.outputs.audio == first.outputs.audio
    assert second.outputs.audio.stat().st_mtime_ns == audio_mtime
    assert second.outputs.audio.read_bytes() == audio_bytes
    assert first.outputs.markdown.read_text(encoding="utf-8") == "Human corrections"
    with pytest.raises(ValidationError, match="--overwrite"):
        run(tmp_path, source)
    assert run(tmp_path, source, overwrite=True).status == "replaced"
    assert first.outputs.audio.stat().st_mtime_ns == audio_mtime
    assert validate_artifact(second.outputs.manifest, verify_source=True)["status"] == "valid"


@pytest.mark.parametrize("overwrite", [False, True])
def test_corrupt_shared_audio_is_never_replaced(tmp_path: Path, overwrite: bool) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    first = run(tmp_path, source)
    first.outputs.audio.write_bytes(b"modified audio")
    with pytest.raises(ValidationError, match="does not match manifest"):
        run(tmp_path, source, profile="local/local-large", overwrite=overwrite)
    assert first.outputs.audio.read_bytes() == b"modified audio"
    with pytest.raises(ValidationError, match="does not match manifest"):
        validate_artifact(first.outputs.manifest, verify_source=True)


def test_missing_audio_is_restored_without_changing_other_results(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    first = run(tmp_path, source)
    expected = first.outputs.audio.read_bytes()
    first.outputs.audio.unlink()
    with pytest.raises(ValidationError, match="--overwrite"):
        run(tmp_path, source)
    assert run(tmp_path, source, overwrite=True).status == "replaced"
    assert first.outputs.audio.read_bytes() == expected


def test_manifest_four_requires_audio_and_legacy_manifests_still_validate(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    result = run(tmp_path, source)
    manifest = json.loads(result.outputs.manifest.read_text(encoding="utf-8"))
    del manifest["outputs"]["audio"]
    target = tmp_path / "outputs" / result.outputs.audio.parent.name / "legacy.json"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValidationError, match="must include audio"):
        validate_artifact(target, verify_source=True)
    del manifest["audio_conversion"]
    for version in (1, 2, 3):
        manifest["schema_version"] = version
        target.write_text(json.dumps(manifest), encoding="utf-8")
        assert validate_artifact(target, verify_source=True)["status"] == "valid"


def test_full_hash_collision_guard_before_processing(tmp_path: Path) -> None:
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"source")
    first = run(tmp_path, source)
    manifest = json.loads(first.outputs.manifest.read_text(encoding="utf-8"))
    manifest["source"]["sha256"] = sha256_file(source)[:12] + "0" * 52
    first.outputs.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValidationError, match="source hash collision"):
        run(tmp_path, source, overwrite=True)


@pytest.mark.parametrize("name", ["日本語", "CON", "a" * 300, "🎤" * 100])
def test_long_unicode_and_reserved_stems_are_safe(tmp_path: Path, name: str) -> None:
    paths = output_paths(
        tmp_path, Path(name + ".mp4"), mode="local", profile="local-small",
        source_hash="a" * 64, fingerprint="b" * 64,
    )
    assert len(paths.audio.parent.name.encode("utf-16-le")) <= 148
    assert paths.audio.parent.parent == tmp_path
    assert paths.audio.parent.name.endswith("_aaaaaaaaaaaa")
    assert paths.markdown.name == "transcript_local_local-small_bbbbbbbbbbbb.md"


def test_copy_verifies_before_publish_and_never_replaces_shared_file(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "audio.flac"
    source.write_bytes(b"audio")
    with pytest.raises(ValidationError, match="Copied audio hash"):
        atomic_copy_file(source, target, expected_hash="incorrect", replace=False)
    assert not target.exists()
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        atomic_copy_file(source, target, expected_hash=sha256_file(source), replace=False)
    assert target.read_bytes() == b"existing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio.flac", "source"]


def test_azure_partial_publication_resumes_without_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"source")
    calls: list[str] = []
    submitted: list[bytes] = []

    class Recognizer(FakeAzureRecognizer):
        def transcribe(self, upload_audio: Path):  # type: ignore[no-untyped-def]
            submitted.append(upload_audio.read_bytes())
            return super().transcribe(upload_audio)

    pipeline = TranscriptionPipeline(
        config=make_azure_config(tmp_path), logger=make_logger(),
        ffmpeg=FakeFfmpeg([]),  # type: ignore[arg-type]
        azure_recognizer_factory=lambda cfg, logger: Recognizer(calls),
    )
    import os

    original_replace = os.replace

    def fail_srt(src: Path, dst: Path) -> None:
        if str(src).endswith(".srt.pending"):
            raise OSError("interrupted publication")
        original_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr("audio_transcriber.pipeline.os.replace", fail_srt)
        with pytest.raises(OSError, match="interrupted publication"):
            pipeline.transcribe(source, dry_run=False, overwrite=False, allow_cloud_upload=True)
    with pytest.raises(ValidationError, match="--overwrite"):
        pipeline.transcribe(source, dry_run=False, overwrite=False, allow_cloud_upload=True)
    result = pipeline.transcribe(source, dry_run=False, overwrite=True, allow_cloud_upload=True)
    assert len(calls) == 1
    assert result.outputs.audio.read_bytes() == submitted[0]
    assert validate_artifact(result.outputs.manifest, verify_source=True)["status"] == "valid"


def test_real_local_pipeline_archives_full_audio(tmp_path: Path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("ffmpeg is required for the full-audio archival test")
    source = tmp_path / "会議.wav"
    _write_wav(source, seconds=20)
    before = source.read_bytes()
    adapter = FfmpegAdapter(executable, 30, make_logger())
    result = TranscriptionPipeline(
        config=make_config(tmp_path), logger=make_logger(), ffmpeg=adapter,
        recognizer_factory=lambda model, cfg: FakeRecognizer([]),
    ).transcribe(source, dry_run=False, overwrite=False)
    decoded = tmp_path / "decoded.wav"
    adapter.normalize(result.outputs.audio, decoded)
    import wave

    with wave.open(str(decoded), "rb") as stream:
        assert stream.getnframes() == 20 * 16000
        assert stream.readframes(stream.getnframes()) == b"\0\0" * 20 * 16000
    assert source.read_bytes() == before
    assert validate_artifact(result.outputs.manifest, verify_source=True)["status"] == "valid"

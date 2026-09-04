from __future__ import annotations

import logging
from pathlib import Path

import pytest

from audio_transcriber.errors import ValidationError
from audio_transcriber.model_store import ensure_local_model


def make_logger() -> logging.Logger:
    logger = logging.getLogger("audio_transcriber.tests.model_store")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def test_ensure_local_model_downloads_missing_known_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path, int]] = []

    def fake_snapshot_download(*, repo_id: str, local_dir: str, max_workers: int) -> None:
        target = Path(local_dir)
        calls.append((repo_id, target, max_workers))
        target.mkdir(parents=True)
        (target / "model.bin").write_bytes(b"model")
        (target / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot_download)
    model_dir = tmp_path / "models"

    resolved = ensure_local_model(
        model="small",
        model_dir=model_dir,
        cache_dir=tmp_path / "cache",
        logger=make_logger(),
    )

    assert resolved == (model_dir / "faster-whisper-small").resolve()
    assert calls == [
        (
            "Systran/faster-whisper-small",
            model_dir / "faster-whisper-small",
            1,
        )
    ]


def test_ensure_local_model_reuses_complete_model_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "models" / "faster-whisper-small"
    target.mkdir(parents=True)
    (target / "model.bin").write_bytes(b"model")
    (target / "config.json").write_text("{}", encoding="utf-8")

    def unexpected_download(**kwargs: object) -> None:
        raise AssertionError(f"download should not be called: {kwargs}")

    monkeypatch.setattr("huggingface_hub.snapshot_download", unexpected_download)

    resolved = ensure_local_model(
        model="small",
        model_dir=tmp_path / "models",
        cache_dir=tmp_path / "cache",
        logger=make_logger(),
    )

    assert resolved == target.resolve()


def test_ensure_local_model_does_not_replace_incomplete_explicit_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit-model"
    explicit.mkdir()
    (explicit / "model.bin").write_bytes(b"partial")

    def unexpected_download(**kwargs: object) -> None:
        raise AssertionError(f"download should not be called: {kwargs}")

    monkeypatch.setattr("huggingface_hub.snapshot_download", unexpected_download)

    with pytest.raises(ValidationError, match="Local model directory is incomplete"):
        ensure_local_model(
            model=str(explicit),
            model_dir=tmp_path / "models",
            cache_dir=tmp_path / "cache",
            logger=make_logger(),
        )

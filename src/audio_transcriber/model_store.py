from __future__ import annotations

import logging
import os
from pathlib import Path

from .errors import ValidationError

KNOWN_MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def configure_huggingface_cache(cache_dir: Path) -> None:
    os.environ.setdefault("HF_HOME", str(cache_dir))
    os.environ.setdefault("HF_HUB_CACHE", str(cache_dir / "hub"))
    os.environ.setdefault("HF_XET_CACHE", str(cache_dir / "xet"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def model_directory_name(model: str, repository_id: str) -> str:
    return f"faster-whisper-{model}" if model in KNOWN_MODELS else repository_id.replace("/", "--")


def _contains_model(path: Path) -> bool:
    return path.is_dir() and (path / "model.bin").is_file()


def resolve_local_model(model: str, model_dir: Path) -> Path:
    explicit = Path(model).expanduser()
    if explicit.exists():
        if not _contains_model(explicit):
            raise ValidationError(f"Local model directory is incomplete: {explicit}")
        return explicit.resolve()
    repository_id = KNOWN_MODELS.get(model, model)
    candidates = (
        model_dir / model_directory_name(model, repository_id),
        model_dir / repository_id.replace("/", "--"),
    )
    for candidate in candidates:
        if _contains_model(candidate):
            return candidate.resolve()
    raise ValidationError(
        f"Model '{model}' is not available under {model_dir}. "
        f"Run 'tkn-audio-transcriber model download {model}'."
    )


def download_model(
    *,
    model: str,
    model_dir: Path,
    cache_dir: Path,
    dry_run: bool,
    logger: logging.Logger,
) -> dict[str, str]:
    repository_id = KNOWN_MODELS.get(model, model)
    target = model_dir / model_directory_name(model, repository_id)
    result = {
        "status": "planned" if dry_run else "created",
        "model": model,
        "repository_id": repository_id,
        "target": str(target),
    }
    if dry_run:
        return result
    if _contains_model(target):
        result["status"] = "unchanged"
        return result
    model_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    configure_huggingface_cache(cache_dir)
    logger.info("Downloading model %s from %s", model, repository_id)
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=repository_id, local_dir=str(target), max_workers=1)
    except Exception as exc:
        raise ValidationError(f"Model download failed: {exc}") from exc
    if not _contains_model(target):
        raise ValidationError(f"Downloaded model is incomplete: {target}")
    return result


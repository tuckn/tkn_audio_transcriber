"""Run transcription and candidate generation, saving files and exiting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import ResolvedConfig
from .errors import AudioTranscriberError, ValidationError
from .phrase_suggestions import preflight_phrase_generation, suggest_phrases
from .pipeline import TranscriptionPipeline


def prepare_transcription(
    audio: Path,
    *,
    config: ResolvedConfig,
    logger: logging.Logger,
    generation_profile: str | None = None,
    context_file: Path | None = None,
    max_candidates: int | None = None,
    dry_run: bool = False,
    allow_cloud_upload: bool = False,
) -> dict[str, Any]:
    root = config.path("output_dir")
    if root is None:
        raise ValidationError("transcribe --suggest-phrases requires an output directory")
    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValidationError("Workflow output path is not a directory")
    generation_plan = preflight_phrase_generation(
        config,
        generation_profile=generation_profile,
        context_file=context_file,
        max_candidates=max_candidates,
    )
    pipeline = TranscriptionPipeline(config=config, logger=logger)
    # Use the same output calculation and validation as ordinary transcription.
    planned = pipeline.transcribe(audio, dry_run=True, overwrite=False)
    vocabulary = planned.outputs.jsonl.parent / "vocabulary"
    if vocabulary.exists() or vocabulary.is_symlink():
        raise ValidationError(
            f"Vocabulary output already exists: {vocabulary}; review the existing files "
            "or choose a new --output-dir. No files were changed."
        )
    logger.info("Step 1/2: %s transcription", "Planning" if dry_run else "Running")
    result = planned if dry_run else pipeline.transcribe(
        audio, dry_run=False, overwrite=False, allow_cloud_upload=allow_cloud_upload,
    )
    vocabulary = result.outputs.jsonl.parent / "vocabulary"
    if dry_run:
        # Defer transcript-dependent validation and estimates to the standalone command.
        return {
            "status": "planned",
            "transcription": result.to_dict(),
            "suggestions": {**generation_plan, "output_dir": str(vocabulary)},
            "network_calls": 0,
            "stop_after": "phrase_review",
        }
    logger.info("Step 2/2: Generating phrase candidates")
    try:
        suggestions = suggest_phrases(
            result.outputs.jsonl,
            output_dir=vocabulary,
            config=config,
            generation_profile=generation_profile,
            context_file=context_file,
            max_candidates=max_candidates,
        )
    except AudioTranscriberError as exc:
        raise AudioTranscriberError(
            f"Transcription was saved at {result.outputs.jsonl}. "
            "Phrase generation failed; rerun the same transcribe --suggest-phrases command "
            "to reuse the transcript, "
            f"or use suggest-phrases separately. {exc}"
        ) from None
    return {
        "status": "review_required",
        "transcription": result.to_dict(),
        "suggestions": suggestions,
        "stop_after": "phrase_review",
    }

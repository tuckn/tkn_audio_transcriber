"""Run transcription and candidate generation, stopping at human review."""

from __future__ import annotations

import logging
from dataclasses import replace
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
        raise ValidationError("prepare requires an output directory")
    root = root.expanduser().resolve()
    vocabulary = root / "vocabulary"
    transcript_dir = root / "transcript"
    if root.exists() and not root.is_dir():
        raise ValidationError("Workflow output path is not a directory")
    if vocabulary.exists() or vocabulary.is_symlink():
        raise ValidationError(
            f"Vocabulary output already exists: {vocabulary}; review the existing files "
            "or choose a new --output-dir. No files were changed."
        )
    if transcript_dir.exists() and not transcript_dir.is_dir():
        raise ValidationError("Transcript output path is not a directory")
    generation_plan = preflight_phrase_generation(
        config,
        generation_profile=generation_profile,
        context_file=context_file,
        max_candidates=max_candidates,
    )
    transcript_config = replace(
        config,
        values={**config.values, "output_dir": str(transcript_dir)},
        sources={**config.sources, "output_dir": "prepare output directory"},
    )
    logger.info("Step 1/2: %s transcription", "Planning" if dry_run else "Running")
    result = TranscriptionPipeline(config=transcript_config, logger=logger).transcribe(
        audio,
        dry_run=dry_run,
        overwrite=False,
        allow_cloud_upload=allow_cloud_upload,
    )
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
            "Phrase generation failed; rerun the same prepare command to reuse the transcript, "
            f"or use phrases suggest separately. {exc}"
        ) from None
    return {
        "status": "review_required",
        "transcription": result.to_dict(),
        "suggestions": suggestions,
        "stop_after": "phrase_review",
    }

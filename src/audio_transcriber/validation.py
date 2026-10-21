from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io_utils import read_json, sha256_file

MANIFEST_SCHEMA_VERSION = 4
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = {1, 2, 3, MANIFEST_SCHEMA_VERSION}

# Bump the conversion contract when extraction or timeline handling changes.
AUDIO_CONVERSION = {
    "version": 1,
    "source_audio_stream": "0:a:0",
    "sample_rate": 16000,
    "channels": 1,
    "sample_width": 2,
    "codec": "flac",
    "scope": "whole_first_audio_stream",
    "timeline": "decoded_audio",
    "time_offset_seconds": 0,
    "silence_removed": False,
    "time_stretch": False,
}


def _validated_file(record: Any, label: str, manifest_dir: Path) -> Path:
    if not isinstance(record, dict):
        raise ValidationError(f"Manifest output record is invalid: {label}")
    raw_path = record.get("path")
    expected_hash = record.get("sha256")
    expected_size = record.get("size")
    if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
        raise ValidationError(f"Manifest output record is incomplete: {label}")
    path = Path(raw_path)
    if not path.is_absolute():
        path = manifest_dir / path
    if not path.is_file():
        raise ValidationError(f"Output is missing: {path}")
    if not isinstance(expected_size, int) or path.stat().st_size != expected_size:
        raise ValidationError(f"Output size does not match manifest: {path}")
    if sha256_file(path) != expected_hash:
        raise ValidationError(f"Output hash does not match manifest: {path}")
    return path


def validate_artifact(manifest_path: Path, *, verify_source: bool) -> dict[str, Any]:
    path = manifest_path.expanduser().resolve()
    if not path.is_file():
        raise ValidationError(f"Manifest not found: {path}")
    try:
        manifest = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read manifest {path}: {exc}") from exc
    schema_version = manifest.get("schema_version")
    if schema_version not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        raise ValidationError(
            f"Unsupported manifest schema_version: {schema_version!r}"
        )
    if schema_version in {2, 3, MANIFEST_SCHEMA_VERSION}:
        provenance = manifest.get("provenance")
        if not isinstance(provenance, dict):
            raise ValidationError(
                f"Manifest provenance must be an object for schema {schema_version}"
            )
        required_provenance = {
            "provider",
            "api_version",
            "region",
            "locale",
            "diarization_enabled",
            "authentication_method",
            "source_sha256",
            "decoded_duration_seconds",
            "attempts",
            "retries",
            "tool_version",
        }
        if schema_version in {3, MANIFEST_SCHEMA_VERSION}:
            required_provenance.add("profile")
        missing = sorted(required_provenance - set(provenance))
        if missing:
            raise ValidationError(
                "Manifest provenance is incomplete: " + ", ".join(missing)
            )
    output_records = manifest.get("outputs")
    if not isinstance(output_records, dict):
        raise ValidationError("Manifest outputs must be an object")
    if schema_version == MANIFEST_SCHEMA_VERSION:
        required = {"audio", "markdown", "srt", "jsonl"}
        if not required.issubset(output_records):
            raise ValidationError("Manifest outputs must include audio, markdown, srt, and jsonl")
        if manifest.get("audio_conversion") != AUDIO_CONVERSION:
            raise ValidationError("Manifest audio conversion is invalid")
    validated = {
        label: str(_validated_file(record, label, path.parent))
        for label, record in output_records.items()
    }
    source_verified = False
    if verify_source:
        source_record = manifest.get("source")
        if not isinstance(source_record, dict):
            raise ValidationError("Manifest source record is invalid")
        source_path_value = source_record.get("path")
        source_hash = source_record.get("sha256")
        if not isinstance(source_path_value, str) or not isinstance(source_hash, str):
            raise ValidationError("Manifest source record is incomplete")
        source_path = Path(source_path_value)
        if not source_path.is_file():
            raise ValidationError(f"Source is missing: {source_path}")
        if sha256_file(source_path) != source_hash:
            raise ValidationError(f"Source hash does not match manifest: {source_path}")
        source_verified = True
    return {
        "status": "valid",
        "manifest": str(path),
        "outputs": validated,
        "source_verified": source_verified,
    }


def shared_audio_record(directory: Path, source_hash: str) -> dict[str, Any] | None:
    """Check shared audio without requiring other, possibly edited, transcripts to validate."""
    expected: dict[str, Any] | None = None
    for path in sorted(directory.glob("transcript_*.manifest.json")):
        try:
            manifest = read_json(path)
        except (OSError, ValueError) as exc:
            raise ValidationError(f"Cannot inspect shared audio manifest: {path}") from exc
        source = manifest.get("source")
        if not isinstance(source, dict) or source.get("sha256") != source_hash:
            raise ValidationError(f"Media folder source hash collision: {directory}")
        if manifest.get("audio_conversion") != AUDIO_CONVERSION:
            raise ValidationError("Shared audio conversion differs; choose a new --output-dir")
        records = manifest.get("outputs")
        record = records.get("audio") if isinstance(records, dict) else None
        if not isinstance(record, dict) or record.get("path") != "audio.flac":
            raise ValidationError(f"Shared audio record is invalid: {path}")
        if not isinstance(record.get("sha256"), str) or not isinstance(record.get("size"), int):
            raise ValidationError(f"Shared audio record is incomplete: {path}")
        if expected is not None and record != expected:
            raise ValidationError(f"Shared audio manifests disagree: {directory}")
        expected = record
    if expected is not None and (directory / "audio.flac").exists():
        _validated_file(expected, "audio", directory)
    return expected

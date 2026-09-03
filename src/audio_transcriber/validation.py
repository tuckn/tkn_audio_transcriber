from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io_utils import read_json, sha256_file

MANIFEST_SCHEMA_VERSION = 2
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = {1, MANIFEST_SCHEMA_VERSION}


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
    if schema_version == MANIFEST_SCHEMA_VERSION:
        provenance = manifest.get("provenance")
        if not isinstance(provenance, dict):
            raise ValidationError("Manifest provenance must be an object for schema 2")
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
        missing = sorted(required_provenance - set(provenance))
        if missing:
            raise ValidationError(
                "Manifest provenance is incomplete: " + ", ".join(missing)
            )
    output_records = manifest.get("outputs")
    if not isinstance(output_records, dict):
        raise ValidationError("Manifest outputs must be an object")
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

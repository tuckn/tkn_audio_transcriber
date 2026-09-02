from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .io_utils import atomic_write_text

APPLICATION_ID = "audio_transcriber"
SCHEMA_VERSION = "1.0.0"
_SCHEMA_VERSION_PARTS = (1, 0, 0)
_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SCHEMA_VERSION_LINE_PATTERN = re.compile(
    r"^(?P<prefix>schema_version\s*:\s*)(?P<value>[^#\r\n]*?)(?P<suffix>\s*(?:#.*)?)$",
    re.MULTILINE,
)
CONFIG_EXAMPLE_RESOURCE = "resources/config.example.yaml"

DEFAULTS: dict[str, Any] = {
    "model": "small",
    "language": "ja",
    "chunk_seconds": 600,
    "beam_size": 1,
    "compute_type": "int8",
    "device": "cpu",
    "output_dir": ".",
    "model_dir": "~/.cache/audio_transcriber/models",
    "cache_dir": "~/.cache/audio_transcriber/huggingface",
    "state_dir": "~/.tkn/audio_transcriber/state",
    "ffmpeg_executable": "ffmpeg",
    "subprocess_timeout_seconds": 3600,
    "heartbeat_seconds": 60,
    "keep_working_files": False,
}

EXPECTED_TYPES: dict[str, type[Any] | tuple[type[Any], ...]] = {
    "model": str,
    "language": str,
    "chunk_seconds": int,
    "beam_size": int,
    "compute_type": str,
    "device": str,
    "output_dir": (str, type(None)),
    "model_dir": str,
    "cache_dir": str,
    "state_dir": str,
    "ffmpeg_executable": str,
    "subprocess_timeout_seconds": int,
    "heartbeat_seconds": int,
    "keep_working_files": bool,
}

PATH_KEYS = {"output_dir", "model_dir", "cache_dir", "state_dir"}


@dataclass(frozen=True)
class ResolvedConfig:
    values: dict[str, Any]
    sources: dict[str, str]
    loaded_files: tuple[Path, ...]
    config_sources: tuple[dict[str, Any], ...]
    effective_schema_version: str = SCHEMA_VERSION

    @property
    def has_in_memory_migrations(self) -> bool:
        return any(source["migration"] is not None for source in self.config_sources)

    def path(self, key: str) -> Path | None:
        value = self.values[key]
        return None if value is None else Path(str(value))

    def display(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "effective_schema_version": self.effective_schema_version,
            "has_in_memory_migrations": self.has_in_memory_migrations,
            "config_sources": [dict(source) for source in self.config_sources],
            "values": {
                key: {"value": value, "source": self.sources[key]}
                for key, value in sorted(self.values.items())
            },
            "loaded_files": [str(path) for path in self.loaded_files],
        }


def default_user_config_path(*, home: Path | None = None) -> Path:
    return (home or Path.home()) / ".tkn" / APPLICATION_ID / "config.yaml"


def config_example_text() -> str:
    resource = files("audio_transcriber").joinpath(CONFIG_EXAMPLE_RESOURCE)
    return resource.read_text(encoding="utf-8")


def _read_yaml(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    return text, dict(loaded)


def _inspect_schema_version(value: dict[str, Any], path: Path) -> dict[str, Any]:
    if "schema_version" not in value:
        raise ConfigError(
            f'schema_version is required in {path}; set schema_version: "{SCHEMA_VERSION}"'
        )

    raw_version = value["schema_version"]
    if type(raw_version) is int and raw_version == _SCHEMA_VERSION_PARTS[0]:
        return {
            "schema_version": raw_version,
            "effective_schema_version": SCHEMA_VERSION,
            "migration": {
                "kind": "legacy_integer_version",
                "from_version": raw_version,
                "to_version": SCHEMA_VERSION,
                "persistent_config_updated": False,
            },
        }

    if not isinstance(raw_version, str) or not _SCHEMA_VERSION_PATTERN.fullmatch(raw_version):
        raise ConfigError(
            f"Invalid schema_version {raw_version!r} in {path}; expected a quoted "
            f'MAJOR.MINOR.PATCH value such as "{SCHEMA_VERSION}"'
        )

    major, minor, _patch = (int(part) for part in raw_version.split("."))
    current_major, current_minor, _current_patch = _SCHEMA_VERSION_PARTS
    if major != current_major:
        direction = "newer" if major > current_major else "older"
        action = (
            "upgrade tkn-audio-transcriber"
            if major > current_major
            else "migrate the configuration explicitly; no migration path is available"
        )
        raise ConfigError(
            f"Unsupported {direction} schema_version {raw_version!r} in {path}; "
            f"this application supports versions through {SCHEMA_VERSION} within "
            f"major {current_major}; {action}"
        )
    if minor > current_minor:
        raise ConfigError(
            f"Unsupported newer schema_version {raw_version!r} in {path}; this "
            f"application supports versions through {SCHEMA_VERSION}; upgrade "
            "tkn-audio-transcriber"
        )

    migration: dict[str, Any] | None = None
    if minor < current_minor:
        migration = {
            "kind": "compatible_version_normalization",
            "from_version": raw_version,
            "to_version": SCHEMA_VERSION,
            "persistent_config_updated": False,
        }
    return {
        "schema_version": raw_version,
        "effective_schema_version": SCHEMA_VERSION,
        "migration": migration,
    }


def _validate(values: dict[str, Any]) -> None:
    for key, value in values.items():
        expected_type = EXPECTED_TYPES[key]
        if isinstance(value, bool) and expected_type is int:
            raise ConfigError(f"{key} must be an integer, not a boolean")
        if not isinstance(value, expected_type):
            raise ConfigError(
                f"{key} has invalid type {type(value).__name__}; expected {expected_type}"
            )
    for key in (
        "chunk_seconds",
        "beam_size",
        "subprocess_timeout_seconds",
        "heartbeat_seconds",
    ):
        if key in values and values[key] <= 0:
            raise ConfigError(f"{key} must be greater than zero")
    for key in ("model", "language", "compute_type", "device", "ffmpeg_executable"):
        if key in values and not values[key].strip():
            raise ConfigError(f"{key} must not be empty")


def _load_yaml(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    _text, loaded = _read_yaml(path)
    schema_report = _inspect_schema_version(loaded, path)
    result = dict(loaded)
    result.pop("schema_version")
    unknown = sorted(set(result) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"Unknown config key(s) in {path}: {', '.join(unknown)}")
    try:
        _validate(result)
    except ConfigError as exc:
        raise ConfigError(f"Invalid config file {path}: {exc}") from exc
    return result, schema_report


def _resolve_paths(values: dict[str, Any], cwd: Path) -> None:
    for key in PATH_KEYS:
        value = values[key]
        if value is None:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        values[key] = str(path.resolve(strict=False))


def resolve_config(
    *,
    cwd: Path,
    explicit_config: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    home: Path | None = None,
) -> ResolvedConfig:
    current_directory = cwd.resolve()
    home_directory = (home or Path.home()).resolve()
    values = dict(DEFAULTS)
    sources = {key: "built-in default" for key in values}
    loaded_files: list[Path] = []

    candidates: list[tuple[Path, str, str, bool]] = [
        (
            home_directory / ".tkn" / APPLICATION_ID / "config.yaml",
            "user",
            "user config",
            False,
        ),
        (
            current_directory / ".tkn" / "config.yaml",
            "working_directory",
            "working-directory config",
            False,
        ),
    ]
    if explicit_config is not None:
        explicit_path = explicit_config.expanduser()
        if not explicit_path.is_absolute():
            explicit_path = current_directory / explicit_path
        candidates.append(
            (explicit_path.resolve(strict=False), "explicit", "explicit config", True)
        )

    config_sources: list[dict[str, Any]] = [
        {
            "kind": "built_in",
            "path": None,
            "exists": True,
            "schema_version": SCHEMA_VERSION,
            "effective_schema_version": SCHEMA_VERSION,
            "migration": None,
        }
    ]
    for path, kind, label, required in candidates:
        source_report: dict[str, Any] = {
            "kind": kind,
            "path": str(path),
            "exists": path.is_file(),
            "schema_version": None,
            "effective_schema_version": None,
            "migration": None,
        }
        config_sources.append(source_report)
        if not path.exists():
            if required:
                raise ConfigError(f"Explicit config file not found: {path}")
            continue
        if not path.is_file():
            raise ConfigError(f"Config path is not a file: {path}")
        layer, schema_report = _load_yaml(path)
        source_report.update(schema_report)
        for key, value in layer.items():
            values[key] = value
            sources[key] = f"{label}: {path}"
        loaded_files.append(path)

    for key, value in (cli_overrides or {}).items():
        if key == "schema_version":
            raise ConfigError(
                "schema_version is configuration-source metadata and cannot be a CLI override"
            )
        if key not in DEFAULTS:
            raise ConfigError(f"Unknown CLI config override: {key}")
        if value is not None:
            values[key] = value
            sources[key] = "CLI option"

    _validate(values)
    _resolve_paths(values, current_directory)
    return ResolvedConfig(
        values=values,
        sources=sources,
        loaded_files=tuple(loaded_files),
        config_sources=tuple(config_sources),
    )


def _next_backup_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.bak")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak.{index}")
        index += 1
    return candidate


def initialize_user_config(
    path: Path | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    target = (path or default_user_config_path()).expanduser().resolve(strict=False)
    example = config_example_text()
    if target.exists() and not target.is_file():
        raise ConfigError(f"Config path is not a file: {target}")
    if target.is_file():
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"Cannot read config file {target}: {exc}") from exc
        if current == example:
            return {
                "status": "unchanged",
                "config_path": str(target),
                "backup_path": None,
            }
        if not force:
            raise ConfigError(
                f"Config already exists with different content: {target}; use --force "
                "to replace it after creating a backup"
            )

    backup_path = _next_backup_path(target) if target.is_file() else None
    if dry_run:
        return {
            "status": "planned",
            "action": "replace" if backup_path else "create",
            "config_path": str(target),
            "backup_path": str(backup_path) if backup_path else None,
        }
    if backup_path is not None:
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            raise ConfigError(f"Cannot back up config file {target}: {exc}") from exc
    try:
        atomic_write_text(target, example)
    except OSError as exc:
        raise ConfigError(f"Cannot write config file {target}: {exc}") from exc
    return {
        "status": "replaced" if backup_path else "created",
        "config_path": str(target),
        "backup_path": str(backup_path) if backup_path else None,
    }


def _replace_schema_version(text: str, path: Path) -> str:
    matches = list(_SCHEMA_VERSION_LINE_PATTERN.finditer(text))
    if len(matches) != 1:
        raise ConfigError(
            f"Cannot safely migrate schema_version in {path}; use one top-level "
            "schema_version mapping entry"
        )
    match = matches[0]
    return (
        text[: match.start()]
        + match.group("prefix")
        + f'"{SCHEMA_VERSION}"'
        + match.group("suffix")
        + text[match.end() :]
    )


def migrate_config_file(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    target = path.expanduser().resolve(strict=False)
    if not target.exists():
        raise ConfigError(f"Config file not found: {target}")
    if not target.is_file():
        raise ConfigError(f"Config path is not a file: {target}")

    text, loaded = _read_yaml(target)
    schema_report = _inspect_schema_version(loaded, target)
    migration = schema_report["migration"]
    if migration is None:
        return {
            "status": "unchanged",
            "config_path": str(target),
            "schema_version": schema_report["schema_version"],
            "effective_schema_version": SCHEMA_VERSION,
            "backup_path": None,
        }

    migrated_text = _replace_schema_version(text, target)
    try:
        migrated = yaml.safe_load(migrated_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Migrated config is not valid YAML: {target}: {exc}") from exc
    if not isinstance(migrated, dict):
        raise ConfigError(f"Migrated config root must be a mapping: {target}")
    migrated_report = _inspect_schema_version(dict(migrated), target)
    if migrated_report["migration"] is not None:
        raise ConfigError(f"Config migration did not reach schema {SCHEMA_VERSION}: {target}")
    migrated_properties = dict(migrated)
    migrated_properties.pop("schema_version")
    unknown = sorted(set(migrated_properties) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"Unknown config key(s) in {target}: {', '.join(unknown)}")
    _validate(migrated_properties)

    backup_path = _next_backup_path(target)
    if dry_run:
        return {
            "status": "planned",
            "config_path": str(target),
            "from_schema_version": schema_report["schema_version"],
            "to_schema_version": SCHEMA_VERSION,
            "backup_path": str(backup_path),
        }
    try:
        shutil.copy2(target, backup_path)
        atomic_write_text(target, migrated_text)
    except OSError as exc:
        raise ConfigError(f"Cannot migrate config file {target}: {exc}") from exc
    return {
        "status": "migrated",
        "config_path": str(target),
        "from_schema_version": schema_report["schema_version"],
        "to_schema_version": SCHEMA_VERSION,
        "backup_path": str(backup_path),
    }

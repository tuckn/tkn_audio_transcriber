from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

APPLICATION_ID = "audio_transcriber"
SCHEMA_VERSION = 1

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

    def path(self, key: str) -> Path | None:
        value = self.values[key]
        return None if value is None else Path(str(value))

    def display(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "values": {
                key: {"value": value, "source": self.sources[key]}
                for key, value in sorted(self.values.items())
            },
            "loaded_files": [str(path) for path in self.loaded_files],
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"Config root must be a mapping: {path}")
    result = dict(loaded)
    schema_version = result.pop("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version {schema_version!r} in {path}; "
            f"supported version is {SCHEMA_VERSION}"
        )
    unknown = sorted(set(result) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"Unknown config key(s) in {path}: {', '.join(unknown)}")
    return result


def _validate(values: dict[str, Any]) -> None:
    for key, expected_type in EXPECTED_TYPES.items():
        value = values[key]
        if isinstance(value, bool) and expected_type is int:
            raise ConfigError(f"{key} must be an integer, not a boolean")
        if not isinstance(value, expected_type):
            raise ConfigError(
                f"{key} has invalid type {type(value).__name__}; "
                f"expected {expected_type}"
            )
    if values["chunk_seconds"] <= 0:
        raise ConfigError("chunk_seconds must be greater than zero")
    if values["beam_size"] <= 0:
        raise ConfigError("beam_size must be greater than zero")
    if values["subprocess_timeout_seconds"] <= 0:
        raise ConfigError("subprocess_timeout_seconds must be greater than zero")
    if values["heartbeat_seconds"] <= 0:
        raise ConfigError("heartbeat_seconds must be greater than zero")
    for key in ("model", "language", "compute_type", "device", "ffmpeg_executable"):
        if not values[key].strip():
            raise ConfigError(f"{key} must not be empty")


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

    candidates: list[tuple[Path, str, bool]] = [
        (
            home_directory / ".tkn" / APPLICATION_ID / "config.yaml",
            "user config",
            False,
        ),
        (current_directory / ".tkn" / "config.yaml", "working-directory config", False),
    ]
    if explicit_config is not None:
        explicit_path = explicit_config.expanduser()
        if not explicit_path.is_absolute():
            explicit_path = current_directory / explicit_path
        candidates.append((explicit_path.resolve(strict=False), "explicit config", True))

    for path, label, required in candidates:
        if not path.exists():
            if required:
                raise ConfigError(f"Explicit config file not found: {path}")
            continue
        if not path.is_file():
            raise ConfigError(f"Config path is not a file: {path}")
        for key, value in _load_yaml(path).items():
            values[key] = value
            sources[key] = f"{label}: {path}"
        loaded_files.append(path)

    for key, value in (cli_overrides or {}).items():
        if key not in DEFAULTS:
            raise ConfigError(f"Unknown CLI config override: {key}")
        if value is not None:
            values[key] = value
            sources[key] = "CLI option"

    _validate(values)
    _resolve_paths(values, current_directory)
    return ResolvedConfig(values=values, sources=sources, loaded_files=tuple(loaded_files))

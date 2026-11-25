from __future__ import annotations

import math
import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

from .errors import ConfigError
from .io_utils import atomic_write_text
from .phrase_files import read_phrase_files, validate_phrase
from .yaml_format import StringDateLoader

APPLICATION_ID = "audio_transcriber"
SCHEMA_VERSION = "5.0.0"
_SCHEMA_VERSION_PARTS = (5, 0, 0)
ARCHIVE_NAME_CREATED_TIME_ORIGINAL = "created-time-original"
_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CONFIG_EXAMPLE_RESOURCE = "resources/config.example.yaml"

LOCAL_PROVIDER = "faster-whisper"
AZURE_PROVIDER = "azure-speech-fast"
PROVIDERS = {LOCAL_PROVIDER, AZURE_PROVIDER}
LOCAL_MODE = "local"
CLOUD_MODE = "cloud"
MODES = {LOCAL_MODE, CLOUD_MODE}
# Where a provider runs. A cloud provider receives audio derived from the media.
MODE_PROVIDERS = {LOCAL_MODE: LOCAL_PROVIDER, CLOUD_MODE: AZURE_PROVIDER}

# Common CLI and folder settings retain a normalized representation. Provider-specific
# execution settings are also materialized as the typed transcription configurations below.
DEFAULTS: dict[str, Any] = {
    "provider": LOCAL_PROVIDER,
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
    "azure_speech_endpoint": None,
    "azure_speech_region": "japaneast",
    "azure_speech_api_version": "2025-10-15",
    "azure_speech_locale": "ja-JP",
    "azure_speech_diarization_enabled": False,
    "azure_speech_profanity_filter_mode": "None",
    "azure_speech_phrases": [],
    "azure_speech_phrase_files": [],
    "azure_speech_reuse_cached_credentials": True,
    "azure_speech_max_speakers": 8,
    "azure_speech_timeout_seconds": 600,
    "azure_speech_max_retries": 3,
}

EXPECTED_TYPES: dict[str, type[Any] | tuple[type[Any], ...]] = {
    "provider": str,
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
    "azure_speech_endpoint": (str, type(None)),
    "azure_speech_region": str,
    "azure_speech_api_version": str,
    "azure_speech_locale": str,
    "azure_speech_diarization_enabled": bool,
    "azure_speech_profanity_filter_mode": str,
    "azure_speech_phrases": list,
    "azure_speech_phrase_files": list,
    "azure_speech_reuse_cached_credentials": bool,
    "azure_speech_max_speakers": int,
    "azure_speech_timeout_seconds": int,
    "azure_speech_max_retries": int,
}

PATH_KEYS = {"output_dir", "model_dir", "cache_dir", "state_dir"}
AZURE_SETTING_KEYS = {
    "azure_speech_profanity_filter_mode",
    "azure_speech_phrases",
    "azure_speech_phrase_files",
    "azure_speech_reuse_cached_credentials",
    "azure_speech_endpoint",
    "azure_speech_region",
    "azure_speech_api_version",
    "azure_speech_locale",
    "azure_speech_diarization_enabled",
    "azure_speech_max_speakers",
    "azure_speech_timeout_seconds",
    "azure_speech_max_retries",
}

LOCAL_PROFILE_DEFAULTS: dict[str, Any] = {
    "model": "small",
    "language": "ja",
    "chunk_seconds": 600,
    "beam_size": 1,
    "compute_type": "int8",
    "device": "cpu",
}
AZURE_PROFILE_DEFAULTS: dict[str, Any] = {
    "endpoint": None,
    "region": "japaneast",
    "api_version": "2025-10-15",
    "locale": "ja-JP",
    "diarization": {"enabled": False, "max_speakers": 8},
    "profanity_filter_mode": "None",
    "phrase_list": {"phrases": [], "files": []},
    "authentication": {"reuse_cached_credentials": True},
    "request": {"timeout_seconds": 600, "max_retries": 3},
}
PROFILE_DEFAULTS = {
    LOCAL_MODE: LOCAL_PROFILE_DEFAULTS,
    CLOUD_MODE: AZURE_PROFILE_DEFAULTS,
}

GENAI_PROFILES_KEY = "genai_profiles"
DEFAULT_GENAI_PROFILE = "codex-default"
# Generation connections, handed to tkn-genai-bridge as Profile objects. The CLI
# providers reuse their own sign-in, so no Bridge configuration file is needed.
BUILT_IN_GENAI_PROFILES: dict[str, dict[str, Any]] = {
    "codex-default": {"provider": "codex", "model": None},
    "claude-default": {"provider": "claude-code", "model": None},
    "copilot-default": {"provider": "github-copilot", "model": None},
    "antigravity-default": {"provider": "antigravity", "model": None},
}

TRANSCRIPTION_PROFILES_KEY = "transcription_profiles"
SOURCES_KEY = "sources"

BUILT_IN_CONFIG: dict[str, Any] = {
    "transcription": {"profile": f"{LOCAL_MODE}/local-small"},
    TRANSCRIPTION_PROFILES_KEY: {
        LOCAL_MODE: {
            "local-small": {"provider": LOCAL_PROVIDER, **deepcopy(LOCAL_PROFILE_DEFAULTS)},
            "local-large": {
                "provider": LOCAL_PROVIDER,
                **deepcopy(LOCAL_PROFILE_DEFAULTS),
                "model": "large-v3",
            },
            "gpu-quality": {
                "provider": LOCAL_PROVIDER,
                **deepcopy(LOCAL_PROFILE_DEFAULTS),
                "model": "large-v3",
                "beam_size": 5,
                "compute_type": "float16",
                "device": "cuda",
            },
            "gpu-fast": {
                "provider": LOCAL_PROVIDER,
                **deepcopy(LOCAL_PROFILE_DEFAULTS),
                "model": "large-v3",
                "compute_type": "int8_float16",
                "device": "cuda",
            },
        },
        CLOUD_MODE: {
            "azure-ja": {"provider": AZURE_PROVIDER, **deepcopy(AZURE_PROFILE_DEFAULTS)},
        },
    },
    "folders": {
        "output": ".",
        "local_models": "~/.cache/audio_transcriber/models",
        "huggingface_cache": "~/.cache/audio_transcriber/huggingface",
        "state": "~/.tkn/audio_transcriber/state",
    },
    "processing": {
        "ffmpeg": {"executable": "ffmpeg", "timeout_seconds": 3600},
        "heartbeat_seconds": 60,
        "keep_working_files": False,
    },
    GENAI_PROFILES_KEY: deepcopy(BUILT_IN_GENAI_PROFILES),
    "generation": {
        "genai_profile": DEFAULT_GENAI_PROFILE,
        "max_candidates": 30,
    },
}

TOP_LEVEL_KEYS = {
    "transcription", TRANSCRIPTION_PROFILES_KEY, "folders", "processing", "generation",
    SOURCES_KEY, GENAI_PROFILES_KEY,
}
SOURCE_KEYS = {
    "input_dir", "include", "min_age_seconds", "profile", "output_dir", "archive", "correction",
}
SOURCE_ARCHIVE_KEYS = {"dir", "name", "delete_source"}
SOURCE_CORRECTION_KEYS = {"context_files"}
SOURCE_ARCHIVE_NAMES = ("created-time", ARCHIVE_NAME_CREATED_TIME_ORIGINAL, "original")
DEFAULT_SOURCE_INCLUDE = (
    "*.flac", "*.m4a", "*.mp3", "*.wav", "*.wma", "*.aac", "*.ogg", "*.opus",
)
DEFAULT_SOURCE_MIN_AGE_SECONDS = 30
SOURCE_PATH_PLACEHOLDERS = {"year"}
_SOURCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]*)\}")
TRANSCRIPTION_KEYS = {"profile"}
PROFILE_KEYS = {
    LOCAL_MODE: {"provider", *LOCAL_PROFILE_DEFAULTS},
    CLOUD_MODE: {"provider", *AZURE_PROFILE_DEFAULTS},
}
FOLDER_KEYS = {"output", "local_models", "huggingface_cache", "state"}
PROCESSING_KEYS = {"ffmpeg", "heartbeat_seconds", "keep_working_files"}
FFMPEG_KEYS = {"executable", "timeout_seconds"}
DIARIZATION_KEYS = {"enabled", "max_speakers"}
REQUEST_KEYS = {"timeout_seconds", "max_retries"}

FOLDER_TO_FLAT = {
    "output": "output_dir",
    "local_models": "model_dir",
    "huggingface_cache": "cache_dir",
    "state": "state_dir",
}
PROCESSING_TO_FLAT = {
    ("ffmpeg", "executable"): "ffmpeg_executable",
    ("ffmpeg", "timeout_seconds"): "subprocess_timeout_seconds",
    ("heartbeat_seconds",): "heartbeat_seconds",
    ("keep_working_files",): "keep_working_files",
}
LOCAL_PROFILE_TO_FLAT = {
    "model": "model",
    "language": "language",
    "chunk_seconds": "chunk_seconds",
    "beam_size": "beam_size",
    "compute_type": "compute_type",
    "device": "device",
}
AZURE_PROFILE_TO_FLAT = {
    ("profanity_filter_mode",): "azure_speech_profanity_filter_mode",
    ("phrase_list", "phrases"): "azure_speech_phrases",
    ("phrase_list", "files"): "azure_speech_phrase_files",
    ("authentication", "reuse_cached_credentials"): "azure_speech_reuse_cached_credentials",
    ("endpoint",): "azure_speech_endpoint",
    ("region",): "azure_speech_region",
    ("api_version",): "azure_speech_api_version",
    ("locale",): "azure_speech_locale",
    ("diarization", "enabled"): "azure_speech_diarization_enabled",
    ("diarization", "max_speakers"): "azure_speech_max_speakers",
    ("request", "timeout_seconds"): "azure_speech_timeout_seconds",
    ("request", "max_retries"): "azure_speech_max_retries",
}



@dataclass(frozen=True)
class LocalTranscriptionConfig:
    mode: Literal["local"]
    profile: str
    provider: str
    model: str
    language: str
    chunk_seconds: int
    beam_size: int
    compute_type: str
    device: str


@dataclass(frozen=True)
class AzureSpeechTranscriptionConfig:
    mode: Literal["cloud"]
    profile: str
    provider: str
    endpoint: str
    region: str
    api_version: str
    locale: str
    diarization_enabled: bool
    max_speakers: int
    timeout_seconds: int
    max_retries: int
    profanity_filter_mode: str = "None"
    phrases: tuple[str, ...] = ()
    reuse_cached_credentials: bool = True
    authentication_dir: Path = Path("~/.tkn/audio_transcriber/state/auth")


type TranscriptionConfig = LocalTranscriptionConfig | AzureSpeechTranscriptionConfig


@dataclass(frozen=True)
class SourceArchiveConfig:
    # Absolute path template; `{year}` is replaced per file.
    directory: str
    name: Literal["created-time", "created-time-original", "original"]
    delete_source: bool


@dataclass(frozen=True)
class SourceCorrectionConfig:
    """Correct each new transcript with generative AI after `pull` transcribes it."""

    # Absolute paths of UTF-8 background text, such as a glossary of the speaker's terms.
    context_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SourceConfig:
    """A folder whose new media files `pull` archives and transcribes."""

    name: str
    input_dir: Path
    include: tuple[str, ...]
    min_age_seconds: int
    profile: str | None
    # Absolute path template; `{year}` is replaced per file.
    output_dir: str
    archive: SourceArchiveConfig | None
    defined_in: str
    correction: SourceCorrectionConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input_dir": str(self.input_dir),
            "include": list(self.include),
            "min_age_seconds": self.min_age_seconds,
            "profile": self.profile,
            "output_dir": self.output_dir,
            "archive": None if self.archive is None else {
                "dir": self.archive.directory,
                "name": self.archive.name,
                "delete_source": self.archive.delete_source,
            },
            "correction": None if self.correction is None else {
                "context_files": [str(path) for path in self.correction.context_files],
            },
            "defined_in": self.defined_in,
        }


@dataclass(frozen=True)
class ResolvedConfig:
    values: dict[str, Any]
    sources: dict[str, str]
    transcription: TranscriptionConfig
    loaded_files: tuple[Path, ...]
    config_sources: tuple[dict[str, Any], ...]
    active_mode: str
    active_mode_source: str
    active_profile: str
    active_profile_source: str
    profiles: tuple[dict[str, Any], ...]
    effective_schema_version: str = SCHEMA_VERSION
    phrase_file_sources: tuple[dict[str, Any], ...] = ()
    generation: dict[str, Any] = field(default_factory=dict)
    generation_sources: dict[str, str] = field(default_factory=dict)
    # Merged connection settings; tkn-genai-bridge validates them when generation runs.
    genai_profiles: dict[str, Any] = field(default_factory=dict)
    genai_profile_sources: dict[str, str] = field(default_factory=dict)
    # Configured `sources` entries (input folders for `pull`), not value origins.
    input_sources: tuple[SourceConfig, ...] = ()

    @property
    def has_in_memory_migrations(self) -> bool:
        return any(source["migration"] is not None for source in self.config_sources)

    def path(self, key: str) -> Path | None:
        value = self.values[key]
        return None if value is None else Path(str(value))

    @property
    def profile_selector(self) -> str:
        return f"{self.active_mode}/{self.active_profile}"

    def profiles_display(self) -> dict[str, Any]:
        return {
            "active_mode": self.active_mode,
            "active_mode_source": self.active_mode_source,
            "active_profile": self.active_profile,
            "active_profile_source": self.active_profile_source,
            "profiles": [dict(profile) for profile in self.profiles],
        }

    def display(self) -> dict[str, Any]:
        hidden_keys = (
            AZURE_SETTING_KEYS
            if isinstance(self.transcription, LocalTranscriptionConfig)
            else set(LOCAL_PROFILE_TO_FLAT.values())
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "effective_schema_version": self.effective_schema_version,
            "has_in_memory_migrations": self.has_in_memory_migrations,
            "active_mode": self.active_mode,
            "active_mode_source": self.active_mode_source,
            "active_profile": self.active_profile,
            "active_profile_source": self.active_profile_source,
            "profiles": [dict(profile) for profile in self.profiles],
            "config_sources": [dict(source) for source in self.config_sources],
            "values": {
                key: {"value": value, "source": self.sources[key]}
                for key, value in sorted(self.values.items())
                if key not in hidden_keys
            },
            "loaded_files": [str(path) for path in self.loaded_files],
            "phrase_files": list(self.phrase_file_sources),
            "generation": self.generation,
            "generation_sources": self.generation_sources,
            "genai_profiles": self.genai_profiles,
            "genai_profile_sources": self.genai_profile_sources,
            "sources": [source.to_dict() for source in self.input_sources],
        }


def default_user_config_path(*, home: Path | None = None) -> Path:
    return (home or Path.home()) / ".tkn" / APPLICATION_ID / "config.yaml"


def config_example_text() -> str:
    resource = files("audio_transcriber").joinpath(CONFIG_EXAMPLE_RESOURCE)
    return resource.read_text(encoding="utf-8")


def _read_yaml(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
        loaded = yaml.load(text, Loader=StringDateLoader)
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
            else "no migration path is available; rewrite the file in the layout that "
            "`tkn-audio-transcriber config init PATH` writes"
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
    return {
        "schema_version": raw_version,
        "effective_schema_version": SCHEMA_VERSION,
        "migration": None,
    }


def _mapping(value: Any, label: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping in {path}")
    return dict(value)


def _reject_unknown(value: dict[str, Any], allowed: set[str], label: str, path: Path) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ConfigError(f"Unknown config key(s) in {path} at {label}: {', '.join(unknown)}")


def _require_type(
    value: Any,
    expected: type[Any] | tuple[type[Any], ...],
    label: str,
    path: Path,
) -> None:
    if isinstance(value, bool) and expected is int:
        raise ConfigError(f"{label} must be an integer, not a boolean in {path}")
    if not isinstance(value, expected):
        raise ConfigError(f"{label} has invalid type {type(value).__name__} in {path}")


def _positive_integer(value: Any, label: str, path: Path, *, allow_zero: bool = False) -> None:
    _require_type(value, int, label, path)
    minimum = 0 if allow_zero else 1
    if value < minimum:
        suffix = "zero or greater" if allow_zero else "greater than zero"
        raise ConfigError(f"{label} must be {suffix} in {path}")


def _validate_profile_values(profile: dict[str, Any], label: str, path: Path) -> None:
    if "profanity_filter_mode" in profile:
        mode = profile["profanity_filter_mode"]
        _require_type(mode, str, f"{label}.profanity_filter_mode", path)
        if mode not in {"None", "Masked", "Removed", "Tags"}:
            raise ConfigError(
                f"{label}.profanity_filter_mode must be None, Masked, Removed, or Tags"
            )
    if "phrase_list" in profile:
        phrase_list = _mapping(
            {} if profile["phrase_list"] is None else profile["phrase_list"],
            f"{label}.phrase_list", path,
        )
        _reject_unknown(phrase_list, {"phrases", "files"}, f"{label}.phrase_list", path)
        if "phrases" in phrase_list:
            _validate_phrases(phrase_list["phrases"])
        if "files" in phrase_list:
            _validate_phrase_paths(phrase_list["files"])
    if "authentication" in profile:
        authentication = _mapping(profile["authentication"], f"{label}.authentication", path)
        _reject_unknown(
            authentication, {"reuse_cached_credentials"}, f"{label}.authentication", path
        )
        if "reuse_cached_credentials" in authentication:
            _require_type(
                authentication["reuse_cached_credentials"],
                bool,
                f"{label}.authentication.reuse_cached_credentials",
                path,
            )
    for key in (
        "model",
        "language",
        "compute_type",
        "device",
        "region",
        "api_version",
        "locale",
    ):
        if key in profile:
            _require_type(profile[key], str, f"{label}.{key}", path)
            if not profile[key].strip():
                raise ConfigError(f"{label}.{key} must not be empty in {path}")
    for key in ("chunk_seconds", "beam_size"):
        if key in profile:
            _positive_integer(profile[key], f"{label}.{key}", path)
    if "endpoint" in profile:
        _require_type(profile["endpoint"], (str, type(None)), f"{label}.endpoint", path)
    if "diarization" in profile:
        diarization = _mapping(profile["diarization"], f"{label}.diarization", path)
        _reject_unknown(diarization, DIARIZATION_KEYS, f"{label}.diarization", path)
        if "enabled" in diarization:
            _require_type(diarization["enabled"], bool, f"{label}.diarization.enabled", path)
        if "max_speakers" in diarization:
            _positive_integer(
                diarization["max_speakers"], f"{label}.diarization.max_speakers", path
            )
            if not 2 <= diarization["max_speakers"] <= 35:
                raise ConfigError(
                    f"{label}.diarization.max_speakers must be between 2 and 35 in {path}"
                )
    if "request" in profile:
        request = _mapping(profile["request"], f"{label}.request", path)
        _reject_unknown(request, REQUEST_KEYS, f"{label}.request", path)
        if "timeout_seconds" in request:
            _positive_integer(request["timeout_seconds"], f"{label}.request.timeout_seconds", path)
        if "max_retries" in request:
            _positive_integer(
                request["max_retries"],
                f"{label}.request.max_retries",
                path,
                allow_zero=True,
            )


def _validate_profile_selector(value: Any, label: str, path: Path) -> None:
    _require_type(value, str, label, path)
    mode, separator, name = value.partition("/")
    if not separator or mode not in MODES or not name.strip():
        raise ConfigError(f"{label} must be local/NAME or cloud/NAME in {path}")


def _validate_profile_provider(
    profile: dict[str, Any], mode: str, label: str, path: Path, *, required: bool
) -> None:
    """A profile names its provider, and the provider must run where its group says."""
    expected = MODE_PROVIDERS[mode]
    if "provider" not in profile:
        if required:
            raise ConfigError(f"{label}.provider is required; set it to {expected!r}")
        return
    if profile["provider"] != expected:
        raise ConfigError(f"{label}.provider must be {expected!r} for a {mode} profile in {path}")


def _validate_named_profiles(value: Any, *, mode: str, path: Path) -> None:
    label = f"{TRANSCRIPTION_PROFILES_KEY}.{mode}"
    profiles = _mapping(value, label, path)
    for raw_name, raw_profile in profiles.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError(f"transcription profile names must be non-empty strings in {path}")
        profile_label = f"{label}.{raw_name}"
        profile = _mapping(raw_profile, profile_label, path)
        _reject_unknown(profile, PROFILE_KEYS[mode], profile_label, path)
        # A later layer may change single fields of an inherited profile without
        # repeating its provider; a complete profile is required when it is resolved.
        _validate_profile_provider(profile, mode, profile_label, path, required=False)
        _validate_profile_values(profile, profile_label, path)


def _validate_path_template(value: Any, label: str, path: Path, *, placeholders: bool) -> None:
    _require_type(value, str, label, path)
    if not value.strip() or "\x00" in value:
        raise ConfigError(f"{label} must be a non-empty path in {path}")
    names = set(_PLACEHOLDER_PATTERN.findall(value))
    allowed = SOURCE_PATH_PLACEHOLDERS if placeholders else set()
    unknown = sorted(names - allowed)
    if unknown:
        accepted = ", ".join("{" + name + "}" for name in sorted(allowed)) or "none"
        raise ConfigError(
            f"{label} uses unsupported placeholder(s) "
            f"{', '.join('{' + name + '}' for name in unknown)} in {path}; accepted: {accepted}"
        )
    if "{" in _PLACEHOLDER_PATTERN.sub("", value) or "}" in _PLACEHOLDER_PATTERN.sub("", value):
        raise ConfigError(f"{label} has an unbalanced brace in {path}")


def _validate_sources(value: Any, path: Path) -> None:
    sources = _mapping({} if value is None else value, SOURCES_KEY, path)
    for raw_name, raw_source in sources.items():
        if not isinstance(raw_name, str) or not _SOURCE_NAME_PATTERN.fullmatch(raw_name):
            raise ConfigError(
                f"Source names must start with a letter or digit and use only letters, "
                f"digits, '.', '_', or '-' in {path}: {raw_name!r}"
            )
        if raw_source is None:
            # A later layer may remove a source inherited from an earlier one.
            continue
        label = f"{SOURCES_KEY}.{raw_name}"
        source = _mapping(raw_source, label, path)
        _reject_unknown(source, SOURCE_KEYS, label, path)
        for required in ("input_dir", "output_dir"):
            if required not in source:
                raise ConfigError(f"{label}.{required} is required in {path}")
        _validate_path_template(source["input_dir"], f"{label}.input_dir", path, placeholders=False)
        _validate_path_template(
            source["output_dir"], f"{label}.output_dir", path, placeholders=True
        )
        if "include" in source:
            include = source["include"]
            if not isinstance(include, list) or not include or any(
                not isinstance(pattern, str) or not pattern.strip() for pattern in include
            ):
                raise ConfigError(f"{label}.include must be a non-empty list of patterns in {path}")
        if "min_age_seconds" in source:
            _positive_integer(
                source["min_age_seconds"], f"{label}.min_age_seconds", path, allow_zero=True
            )
        if "profile" in source and source["profile"] is not None:
            _require_type(source["profile"], str, f"{label}.profile", path)
            if not source["profile"].strip():
                raise ConfigError(f"{label}.profile must not be empty in {path}")
        if "archive" in source and source["archive"] is not None:
            archive = _mapping(source["archive"], f"{label}.archive", path)
            _reject_unknown(archive, SOURCE_ARCHIVE_KEYS, f"{label}.archive", path)
            if "dir" not in archive:
                raise ConfigError(f"{label}.archive.dir is required in {path}")
            _validate_path_template(
                archive["dir"], f"{label}.archive.dir", path, placeholders=True
            )
            if "name" in archive and archive["name"] not in SOURCE_ARCHIVE_NAMES:
                raise ConfigError(
                    f"{label}.archive.name must be one of "
                    f"{', '.join(SOURCE_ARCHIVE_NAMES)} in {path}"
                )
            if "delete_source" in archive:
                _require_type(
                    archive["delete_source"], bool, f"{label}.archive.delete_source", path
                )
        if "correction" in source and source["correction"] is not None:
            # `correction: {}` turns correction on without background text.
            correction = _mapping(source["correction"], f"{label}.correction", path)
            _reject_unknown(correction, SOURCE_CORRECTION_KEYS, f"{label}.correction", path)
            files = correction.get("context_files", [])
            if not isinstance(files, list) or any(
                not isinstance(item, str) or not item.strip() or "\x00" in item for item in files
            ):
                raise ConfigError(
                    f"{label}.correction.context_files must be a list of non-empty paths in {path}"
                )


def _validate_layer(value: dict[str, Any], path: Path) -> None:
    _reject_unknown(value, TOP_LEVEL_KEYS, "root", path)
    if SOURCES_KEY in value:
        _validate_sources(value[SOURCES_KEY], path)
    if "transcription" in value:
        transcription = _mapping(value["transcription"], "transcription", path)
        _reject_unknown(transcription, TRANSCRIPTION_KEYS, "transcription", path)
        if "profile" in transcription:
            _validate_profile_selector(transcription["profile"], "transcription.profile", path)
    if TRANSCRIPTION_PROFILES_KEY in value:
        groups = _mapping(value[TRANSCRIPTION_PROFILES_KEY], TRANSCRIPTION_PROFILES_KEY, path)
        _reject_unknown(groups, MODES, TRANSCRIPTION_PROFILES_KEY, path)
        for mode in (LOCAL_MODE, CLOUD_MODE):
            if mode in groups:
                _validate_named_profiles(groups[mode], mode=mode, path=path)
    if GENAI_PROFILES_KEY in value:
        _validate_genai_profiles(value[GENAI_PROFILES_KEY], path)
    _validate_shared_layer(value, path)


def _require_plain_values(
    value: Any, label: str, path: Path, parents: tuple[int, ...] = ()
) -> None:
    """Keep values `config list` can print: YAML scalars, lists, and string-keyed mappings."""
    if isinstance(value, (dict, list)):
        if id(value) in parents:
            raise ConfigError(f"{label} must not contain itself through a YAML alias in {path}")
        parents = (*parents, id(value))
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError(f"{label} keys must be strings in {path}")
            _require_plain_values(key, f"{label} key", path)
            _require_plain_values(item, f"{label}.{key}", path, parents)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_plain_values(item, f"{label}[{index}]", path, parents)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"{label} must be a finite number in {path}")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # For example, a lone surrogate written as a "\ud83d" escape.
            raise ConfigError(
                f"{label} has text that cannot be written as UTF-8 in {path}"
            ) from None
    elif value is not None and not isinstance(value, (bool, int)):
        raise ConfigError(f"{label} has an unsupported YAML type in {path}")


def _validate_genai_profiles(value: Any, path: Path) -> None:
    """Check the shape only; tkn-genai-bridge validates the fields when generation runs."""
    profiles = _mapping(value, GENAI_PROFILES_KEY, path)
    for raw_name, raw_profile in profiles.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError(f"{GENAI_PROFILES_KEY} names must be non-empty strings in {path}")
        _require_plain_values(raw_name, f"{GENAI_PROFILES_KEY} name", path)
        label = f"{GENAI_PROFILES_KEY}.{raw_name}"
        profile = _mapping(raw_profile, label, path)
        _require_plain_values(profile, label, path)
        cli = profile.get("cli")
        if cli is not None:
            cli = _mapping(cli, f"{label}.cli", path)
            executable = cli.get("executable")
            if executable is not None:
                _require_type(executable, str, f"{label}.cli.executable", path)
                if Path(executable).drive and not Path(executable).is_absolute():
                    # `D:codex.exe` depends on the current folder of drive D:.
                    raise ConfigError(
                        f"{label}.cli.executable must be an absolute path, a path relative "
                        f"to this config file, or a command name on PATH in {path}"
                    )


def _validate_shared_layer(value: dict[str, Any], path: Path) -> None:
    if "generation" in value:
        generation = _mapping(value["generation"], "generation", path)
        _reject_unknown(generation, {"genai_profile", "max_candidates"}, "generation", path)
        if "genai_profile" in generation:
            genai_profile = generation["genai_profile"]
            if not isinstance(genai_profile, str) or not genai_profile.strip():
                raise ConfigError("generation.genai_profile must be a non-empty string")
            _require_plain_values(genai_profile, "generation.genai_profile", path)
        if "max_candidates" in generation:
            _positive_integer(generation["max_candidates"], "generation.max_candidates", path)
            if generation["max_candidates"] > 500:
                raise ConfigError("generation.max_candidates must be at most 500")
    if "folders" in value:
        folders = _mapping(value["folders"], "folders", path)
        _reject_unknown(folders, FOLDER_KEYS, "folders", path)
        for key, item in folders.items():
            expected: type[Any] | tuple[type[Any], ...] = (
                (str, type(None)) if key == "output" else str
            )
            _require_type(item, expected, f"folders.{key}", path)
    if "processing" in value:
        processing = _mapping(value["processing"], "processing", path)
        _reject_unknown(processing, PROCESSING_KEYS, "processing", path)
        if "ffmpeg" in processing:
            ffmpeg = _mapping(processing["ffmpeg"], "processing.ffmpeg", path)
            _reject_unknown(ffmpeg, FFMPEG_KEYS, "processing.ffmpeg", path)
            if "executable" in ffmpeg:
                _require_type(ffmpeg["executable"], str, "processing.ffmpeg.executable", path)
                if not ffmpeg["executable"].strip():
                    raise ConfigError(f"processing.ffmpeg.executable must not be empty in {path}")
            if "timeout_seconds" in ffmpeg:
                _positive_integer(
                    ffmpeg["timeout_seconds"], "processing.ffmpeg.timeout_seconds", path
                )
        if "heartbeat_seconds" in processing:
            _positive_integer(processing["heartbeat_seconds"], "processing.heartbeat_seconds", path)
        if "keep_working_files" in processing:
            _require_type(
                processing["keep_working_files"], bool, "processing.keep_working_files", path
            )


def _validate_phrases(phrases: Any) -> None:
    if not isinstance(phrases, list) or len(phrases) > 500:
        raise ConfigError("phrase_list.phrases must be a list of at most 500 phrases")
    if any(not isinstance(phrase, str) or not phrase.strip() for phrase in phrases):
        raise ConfigError("phrase_list.phrases must contain non-empty strings")
    for phrase in phrases:
        validate_phrase(phrase)


def _validate_phrase_paths(paths: Any) -> None:
    if not isinstance(paths, list) or any(
        not isinstance(path, str) or not path.strip() or "\x00" in path for path in paths
    ):
        raise ConfigError("phrase_list.files must be a list of non-empty paths")


def _validate_flat_values(
    values: dict[str, Any], *, require_provider_settings: bool = False
) -> None:
    azure_active = values.get("provider") == AZURE_PROVIDER
    if azure_active:
        _validate_phrases(values["azure_speech_phrases"])
        _validate_phrase_paths(values["azure_speech_phrase_files"])
        if values["azure_speech_profanity_filter_mode"] not in (
            "None",
            "Masked",
            "Removed",
            "Tags",
        ):
            raise ConfigError(
                "azure_speech_profanity_filter_mode must be None, Masked, Removed, or Tags"
            )
    for key, value in values.items():
        if key in AZURE_SETTING_KEYS and not azure_active:
            continue
        expected_type = EXPECTED_TYPES[key]
        if isinstance(value, bool) and expected_type is int:
            raise ConfigError(f"{key} must be an integer, not a boolean")
        if not isinstance(value, expected_type):
            raise ConfigError(
                f"{key} has invalid type {type(value).__name__}; expected {expected_type}"
            )
    if (
        azure_active
        and values["azure_speech_phrases"]
        and values["azure_speech_api_version"] < "2025-10-15"
    ):
        raise ConfigError("phrase_list requires Azure Speech API version 2025-10-15 or later")
    for key in (
        "chunk_seconds",
        "beam_size",
        "subprocess_timeout_seconds",
        "heartbeat_seconds",
        "azure_speech_timeout_seconds",
    ):
        if (key not in AZURE_SETTING_KEYS or azure_active) and values[key] <= 0:
            raise ConfigError(f"{key} must be greater than zero")
    if azure_active and values["azure_speech_max_retries"] < 0:
        raise ConfigError("azure_speech_max_retries must be zero or greater")
    if azure_active and not 2 <= values["azure_speech_max_speakers"] <= 35:
        raise ConfigError("azure_speech_max_speakers must be between 2 and 35")
    for key in (
        "provider",
        "model",
        "language",
        "compute_type",
        "device",
        "ffmpeg_executable",
        "azure_speech_region",
        "azure_speech_api_version",
        "azure_speech_locale",
    ):
        if (key not in AZURE_SETTING_KEYS or azure_active) and not values[key].strip():
            raise ConfigError(f"{key} must not be empty")
    if values["provider"] not in PROVIDERS:
        raise ConfigError(f"provider must be either {LOCAL_PROVIDER!r} or {AZURE_PROVIDER!r}")
    if require_provider_settings and azure_active:
        endpoint = values["azure_speech_endpoint"]
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ConfigError(
                "azure_speech_endpoint is required when provider is azure-speech-fast"
            )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or "<" in endpoint
            or ">" in endpoint
        ):
            raise ConfigError(
                "azure_speech_endpoint must be a concrete HTTPS origin without "
                "credentials, an extra path, a query, or a fragment"
            )


def _load_yaml(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    _text, loaded = _read_yaml(path)
    schema_report = _inspect_schema_version(loaded, path)
    result = dict(loaded)
    result.pop("schema_version")
    try:
        _validate_layer(result, path)
    except ConfigError as exc:
        raise ConfigError(f"Invalid config file {path}: {exc}") from exc
    return result, schema_report


def _resolve_genai_executables(layer: dict[str, Any], base: Path) -> None:
    """Resolve executable paths from the file that set them; bare names still use PATH."""
    profiles = layer.get(GENAI_PROFILES_KEY)
    if not isinstance(profiles, dict):
        return
    for profile in profiles.values():
        cli = profile.get("cli") if isinstance(profile, dict) else None
        if not isinstance(cli, dict):
            continue
        executable = cli.get("executable")
        if isinstance(executable, str) and (
            "/" in executable or "\\" in executable or executable.startswith("~")
        ):
            try:
                path = Path(executable).expanduser()
            except RuntimeError:
                # `~other-user` may not expand here. Leave it for the provider to report
                # only if this connection is used, instead of failing every command.
                continue
            cli["executable"] = str(path if path.is_absolute() else (base / path).resolve())


def _record_sources(
    value: Any,
    sources: dict[tuple[str, ...], str],
    label: str,
    prefix: tuple[str, ...] = (),
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _record_sources(child, sources, label, (*prefix, str(key)))
    else:
        sources[prefix] = label


def _merge_config(
    target: dict[str, Any],
    layer: dict[str, Any],
    sources: dict[tuple[str, ...], str],
    label: str,
    prefix: tuple[str, ...] = (),
) -> None:
    for key, value in layer.items():
        if key == "phrase_list" and value is None:
            # A commented-out YAML block explicitly clears inherited vocabulary.
            value = {"phrases": [], "files": []}
        key_path = (*prefix, str(key))
        if isinstance(value, dict):
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = {}
                target[key] = existing
            _merge_config(existing, value, sources, label, key_path)
            if not existing:
                # An empty mapping is itself the effective value this layer set.
                sources[key_path] = label
        else:
            target[key] = deepcopy(value)
            sources[key_path] = label


def _profile_modes(config: dict[str, Any], profile_name: str) -> list[str]:
    groups = _mapping(
        config.get(TRANSCRIPTION_PROFILES_KEY), TRANSCRIPTION_PROFILES_KEY, Path("<merged>")
    )
    return [
        mode
        for mode in (LOCAL_MODE, CLOUD_MODE)
        if isinstance(groups.get(mode), dict) and profile_name in groups[mode]
    ]


def _effective_profile(
    config: dict[str, Any], mode: str, name: str, path: Path
) -> tuple[str, dict[str, Any]]:
    groups = _mapping(config.get(TRANSCRIPTION_PROFILES_KEY), TRANSCRIPTION_PROFILES_KEY, path)
    group_label = f"{TRANSCRIPTION_PROFILES_KEY}.{mode}"
    profiles = _mapping(groups.get(mode), group_label, path)
    if name not in profiles:
        available = ", ".join(sorted(str(item) for item in profiles)) or "(none)"
        raise ConfigError(
            f"Unknown transcription profile {mode}/{name!s}; available {mode} profiles: {available}"
        )
    label = f"{group_label}.{name}"
    profile = _mapping(profiles[name], label, path)
    _reject_unknown(profile, PROFILE_KEYS[mode], label, path)
    _validate_profile_provider(profile, mode, label, path, required=True)
    effective = deepcopy(PROFILE_DEFAULTS[mode])
    throwaway_sources: dict[tuple[str, ...], str] = {}
    settings = {key: value for key, value in profile.items() if key != "provider"}
    _merge_config(effective, settings, throwaway_sources, "profile")
    _validate_profile_values(effective, label, path)
    return str(profile["provider"]), effective


def _has_leaf(value: dict[str, Any], keys: tuple[str, ...]) -> bool:
    cursor: Any = value
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            return False
        cursor = cursor[key]
    return not isinstance(cursor, dict) or not cursor


def _get_nested(value: dict[str, Any], keys: tuple[str, ...]) -> Any:
    cursor: Any = value
    for key in keys:
        cursor = cursor[key]
    return cursor


def _select_profile(config: dict[str, Any], selector: str | None, path: Path) -> tuple[str, str]:
    if selector is None:
        # Every layer is validated, so the merged value is always MODE/NAME.
        transcription = _mapping(config.get("transcription"), "transcription", path)
        selector = str(transcription.get("profile"))

    if "/" in selector:
        mode, name = selector.split("/", maxsplit=1)
        if mode not in MODES or not name:
            raise ConfigError(
                f"Invalid transcription profile selector {selector!r}; expected "
                "local/NAME or cloud/NAME"
            )
        return mode, name

    matches = _profile_modes(config, selector)
    if not matches:
        raise ConfigError(f"Unknown transcription profile {selector!r}")
    if len(matches) > 1:
        choices = ", ".join(f"{mode}/{selector}" for mode in matches)
        raise ConfigError(f"Ambiguous transcription profile {selector!r}; use one of: {choices}")
    return matches[0], selector


def _normalized_values(
    config: dict[str, Any],
    source_paths: dict[tuple[str, ...], str],
    mode: str,
    profile_name: str,
    validation_path: Path,
) -> tuple[dict[str, Any], dict[str, str], tuple[dict[str, Any], ...]]:
    groups = _mapping(
        config.get(TRANSCRIPTION_PROFILES_KEY), TRANSCRIPTION_PROFILES_KEY, validation_path
    )
    summaries: list[dict[str, Any]] = []
    for candidate_mode in (LOCAL_MODE, CLOUD_MODE):
        profiles = _mapping(
            groups.get(candidate_mode),
            f"{TRANSCRIPTION_PROFILES_KEY}.{candidate_mode}",
            validation_path,
        )
        for name in sorted(profiles):
            provider, _effective = _effective_profile(
                config, candidate_mode, str(name), validation_path
            )
            summaries.append(
                {
                    "mode": candidate_mode,
                    "name": str(name),
                    "selector": f"{candidate_mode}/{name}",
                    "provider": provider,
                    "active": candidate_mode == mode and str(name) == profile_name,
                }
            )

    provider, profile = _effective_profile(config, mode, profile_name, validation_path)
    profile_path = (TRANSCRIPTION_PROFILES_KEY, mode, profile_name)
    values = dict(DEFAULTS)
    value_sources = {key: "built-in default" for key in values}
    values["provider"] = provider
    value_sources["provider"] = source_paths.get((*profile_path, "provider"), "built-in default")
    if mode == LOCAL_MODE:
        for profile_key, flat_key in LOCAL_PROFILE_TO_FLAT.items():
            values[flat_key] = profile[profile_key]
            value_sources[flat_key] = source_paths.get(
                (*profile_path, profile_key), "built-in default"
            )
    else:
        for azure_path, flat_key in AZURE_PROFILE_TO_FLAT.items():
            values[flat_key] = _get_nested(profile, azure_path)
            value_sources[flat_key] = source_paths.get(
                (*profile_path, *azure_path), "built-in default"
            )

    folders = _mapping(config.get("folders"), "folders", validation_path)
    for folder_key, flat_key in FOLDER_TO_FLAT.items():
        values[flat_key] = folders[folder_key]
        value_sources[flat_key] = source_paths.get(("folders", folder_key), "built-in default")
    processing = _mapping(config.get("processing"), "processing", validation_path)
    for processing_path, flat_key in PROCESSING_TO_FLAT.items():
        values[flat_key] = _get_nested(processing, processing_path)
        value_sources[flat_key] = source_paths.get(
            ("processing", *processing_path), "built-in default"
        )
    return values, value_sources, tuple(summaries)


def _typed_transcription(
    mode: str, profile_name: str, values: dict[str, Any]
) -> TranscriptionConfig:
    if mode == LOCAL_MODE:
        return LocalTranscriptionConfig(
            mode="local",
            profile=profile_name,
            provider=str(values["provider"]),
            model=str(values["model"]),
            language=str(values["language"]),
            chunk_seconds=int(values["chunk_seconds"]),
            beam_size=int(values["beam_size"]),
            compute_type=str(values["compute_type"]),
            device=str(values["device"]),
        )
    endpoint = values["azure_speech_endpoint"]
    assert isinstance(endpoint, str)
    return AzureSpeechTranscriptionConfig(
        mode="cloud",
        profile=profile_name,
        provider=str(values["provider"]),
        endpoint=endpoint,
        region=str(values["azure_speech_region"]),
        api_version=str(values["azure_speech_api_version"]),
        locale=str(values["azure_speech_locale"]),
        diarization_enabled=bool(values["azure_speech_diarization_enabled"]),
        max_speakers=int(values["azure_speech_max_speakers"]),
        timeout_seconds=int(values["azure_speech_timeout_seconds"]),
        max_retries=int(values["azure_speech_max_retries"]),
        profanity_filter_mode=str(values["azure_speech_profanity_filter_mode"]),
        phrases=tuple(values["azure_speech_phrases"]),
        reuse_cached_credentials=bool(values["azure_speech_reuse_cached_credentials"]),
        authentication_dir=Path(values["state_dir"]) / "auth",
    )


def _absolute_template(value: str, base: Path) -> str:
    # Placeholders stay in the string; only the base directory is applied here.
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(Path(os.path.normpath(path)))


def _build_source(name: str, entry: dict[str, Any], base: Path, label: str) -> SourceConfig:
    archive_entry = entry.get("archive")
    archive = None
    if archive_entry is not None:
        archive = SourceArchiveConfig(
            directory=_absolute_template(str(archive_entry["dir"]), base),
            name=archive_entry.get("name", SOURCE_ARCHIVE_NAMES[0]),
            delete_source=bool(archive_entry.get("delete_source", False)),
        )
    profile = entry.get("profile")
    correction_entry = entry.get("correction")
    correction = None
    if correction_entry is not None:
        correction = SourceCorrectionConfig(
            context_files=tuple(
                Path(_absolute_template(str(item), base))
                for item in correction_entry.get("context_files", [])
            )
        )
    return SourceConfig(
        name=name,
        input_dir=Path(_absolute_template(str(entry["input_dir"]), base)),
        include=tuple(entry.get("include", DEFAULT_SOURCE_INCLUDE)),
        min_age_seconds=int(entry.get("min_age_seconds", DEFAULT_SOURCE_MIN_AGE_SECONDS)),
        profile=None if profile is None else str(profile),
        output_dir=_absolute_template(str(entry["output_dir"]), base),
        archive=archive,
        defined_in=label,
        correction=correction,
    )


def _apply_sources_layer(
    sources: dict[str, SourceConfig], layer: Any, path: Path, label: str
) -> None:
    # A source entry replaces an inherited one as a whole and keeps its own
    # config directory as the base for relative paths.
    for name, entry in (layer or {}).items():
        sources.pop(str(name), None)
        if entry is not None:
            sources[str(name)] = _build_source(str(name), entry, path.parent, label)


def _config_candidates(
    current_directory: Path, home_directory: Path, explicit_config: Path | None
) -> list[tuple[Path, str, str, bool]]:
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
    return candidates


def load_input_sources(
    *, cwd: Path, explicit_config: Path | None = None, home: Path | None = None
) -> tuple[SourceConfig, ...]:
    """Read `sources` without resolving the active transcription profile.

    `pull` uses each source's own profile, so an unused active profile that is
    incomplete (for example a cloud profile without an endpoint) must not block it.
    Each file is still validated as a whole.
    """
    current_directory = cwd.resolve()
    home_directory = (home or Path.home()).resolve()
    sources: dict[str, SourceConfig] = {}
    for path, _kind, label, required in _config_candidates(
        current_directory, home_directory, explicit_config
    ):
        if not path.exists():
            if required:
                raise ConfigError(f"Explicit config file not found: {path}")
            continue
        if not path.is_file():
            raise ConfigError(f"Config path is not a file: {path}")
        layer, _schema_report = _load_yaml(path)
        _apply_sources_layer(sources, layer.get(SOURCES_KEY), path, f"{label}: {path}")
    return tuple(sources.values())


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
    profile: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    home: Path | None = None,
    phrase_files: list[Path] | None = None,
) -> ResolvedConfig:
    current_directory = cwd.resolve()
    home_directory = (home or Path.home()).resolve()
    config = deepcopy(BUILT_IN_CONFIG)
    source_paths: dict[tuple[str, ...], str] = {}
    _record_sources(config, source_paths, "built-in default")
    loaded_files: list[Path] = []
    source_directories: dict[str, Path] = {"built-in default": current_directory}
    media_sources: dict[str, SourceConfig] = {}

    candidates = _config_candidates(current_directory, home_directory, explicit_config)

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
        source_label = f"{label}: {path}"
        source_directories[source_label] = path.parent
        _apply_sources_layer(media_sources, layer.pop(SOURCES_KEY, None), path, source_label)
        _resolve_genai_executables(layer, path.parent)
        _merge_config(config, layer, source_paths, source_label)
        loaded_files.append(path)

    selected_mode, selected_profile = _select_profile(config, profile, current_directory)
    # One selector names both, so the mode and the profile always come from the same place.
    active_mode_source = active_profile_source = (
        "CLI option"
        if profile is not None
        else source_paths.get(("transcription", "profile"), "built-in default")
    )
    values, sources, profile_summaries = _normalized_values(
        config, source_paths, selected_mode, selected_profile, current_directory
    )
    for key, value in (cli_overrides or {}).items():
        if key == "schema_version":
            raise ConfigError(
                "schema_version is configuration-source metadata and cannot be a CLI override"
            )
        if key not in DEFAULTS:
            raise ConfigError(f"Unknown CLI config override: {key}")
        if value is not None:
            if key == "provider":
                raise ConfigError(
                    "provider cannot be overridden independently; select a local/NAME "
                    "or cloud/NAME profile with --profile"
                )
            if selected_mode == LOCAL_MODE and key in AZURE_SETTING_KEYS:
                raise ConfigError(
                    f"{key} is a cloud-only option but the selected profile is "
                    f"{selected_mode}/{selected_profile}"
                )
            if selected_mode == CLOUD_MODE and key in set(LOCAL_PROFILE_TO_FLAT.values()):
                raise ConfigError(
                    f"{key} is a local-only option but the selected profile is "
                    f"{selected_mode}/{selected_profile}"
                )
            values[key] = value
            sources[key] = "CLI option"

    _validate_flat_values(values, require_provider_settings=True)
    phrase_records: tuple[dict[str, Any], ...] = ()
    if phrase_files and selected_mode != CLOUD_MODE:
        raise ConfigError("--phrase-file requires a cloud transcription profile")
    if selected_mode == CLOUD_MODE:
        file_key = "azure_speech_phrase_files"
        base = source_directories.get(sources[file_key], current_directory)
        configured_paths = [Path(item).expanduser() for item in values[file_key]]
        resolved_paths = [
            (item if item.is_absolute() else base / item).resolve()
            for item in configured_paths
        ]
        for item in phrase_files or []:
            item = item.expanduser()
            resolved_paths.append(
                (item if item.is_absolute() else current_directory / item).resolve()
            )
        values[file_key] = [str(item) for item in dict.fromkeys(resolved_paths)]
        if phrase_files:
            sources[file_key] += " + CLI --phrase-file"
        values["azure_speech_phrases"], phrase_records = read_phrase_files(
            values["azure_speech_phrases"], resolved_paths
        )
        if resolved_paths:
            sources["azure_speech_phrases"] += " + phrase files"
        _validate_flat_values(values, require_provider_settings=True)
    _resolve_paths(values, current_directory)
    transcription_config = _typed_transcription(selected_mode, selected_profile, values)
    return ResolvedConfig(
        values=values,
        sources=sources,
        transcription=transcription_config,
        loaded_files=tuple(loaded_files),
        config_sources=tuple(config_sources),
        active_mode=selected_mode,
        active_mode_source=active_mode_source,
        active_profile=selected_profile,
        active_profile_source=active_profile_source,
        profiles=profile_summaries,
        phrase_file_sources=phrase_records,
        generation=deepcopy(config["generation"]),
        generation_sources={
            ".".join(key[1:]): source
            for key, source in source_paths.items()
            if key[0] == "generation" and _has_leaf(config, key)
        },
        genai_profiles=deepcopy(config[GENAI_PROFILES_KEY]),
        genai_profile_sources={
            ".".join(key[1:]): source
            for key, source in source_paths.items()
            # A later `cli: null` removes inherited fields; drop their stale origins.
            if key[0] == GENAI_PROFILES_KEY and _has_leaf(config, key)
        },
        input_sources=tuple(media_sources.values()),
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


def migrate_config_file(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Report that one config file is already written in the current schema.

    No older schema has a migration path: loading rejects it with the action to take,
    so a file that loads needs no change, and `dry_run` has nothing to hold back.
    Only this file is checked. What it means together with the other config files,
    such as a profile whose provider none of them sets, is checked by `config list`.
    """
    target = path.expanduser().resolve(strict=False)
    if not target.exists():
        raise ConfigError(f"Config file not found: {target}")
    if not target.is_file():
        raise ConfigError(f"Config path is not a file: {target}")
    _layer, schema_report = _load_yaml(target)
    return {
        "status": "unchanged",
        "config_path": str(target),
        "schema_version": schema_report["schema_version"],
        "effective_schema_version": SCHEMA_VERSION,
        "backup_path": None,
    }

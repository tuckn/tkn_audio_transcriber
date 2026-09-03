from __future__ import annotations

import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

from .errors import ConfigError
from .io_utils import atomic_write_text

APPLICATION_ID = "audio_transcriber"
SCHEMA_VERSION = "3.0.0"
_SCHEMA_VERSION_PARTS = (3, 0, 0)
_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CONFIG_EXAMPLE_RESOURCE = "config.example.yaml"

LOCAL_PROVIDER = "faster-whisper"
AZURE_PROVIDER = "azure-speech-fast"
PROVIDERS = {LOCAL_PROVIDER, AZURE_PROVIDER}
LOCAL_MODE = "local"
CLOUD_MODE = "cloud"
MODES = {LOCAL_MODE, CLOUD_MODE}

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
    "azure_speech_diarization_enabled": True,
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
    "azure_speech_max_speakers": int,
    "azure_speech_timeout_seconds": int,
    "azure_speech_max_retries": int,
}

PATH_KEYS = {"output_dir", "model_dir", "cache_dir", "state_dir"}
AZURE_SETTING_KEYS = {
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
    "diarization": {"enabled": True, "max_speakers": 8},
    "request": {"timeout_seconds": 600, "max_retries": 3},
}
PROFILE_DEFAULTS = {
    LOCAL_MODE: LOCAL_PROFILE_DEFAULTS,
    CLOUD_MODE: AZURE_PROFILE_DEFAULTS,
}

BUILT_IN_CONFIG: dict[str, Any] = {
    "transcription": {
        "active": {"mode": LOCAL_MODE, "profile": "local-small"},
        "local": {
            "provider": LOCAL_PROVIDER,
            "profiles": {
                "local-small": deepcopy(LOCAL_PROFILE_DEFAULTS),
                "local-large": {
                    **deepcopy(LOCAL_PROFILE_DEFAULTS),
                    "model": "large-v3",
                },
                "gpu-quality": {
                    **deepcopy(LOCAL_PROFILE_DEFAULTS),
                    "model": "large-v3",
                    "beam_size": 5,
                    "compute_type": "float16",
                    "device": "cuda",
                },
                "gpu-fast": {
                    **deepcopy(LOCAL_PROFILE_DEFAULTS),
                    "model": "large-v3",
                    "compute_type": "int8_float16",
                    "device": "cuda",
                },
            },
        },
        "cloud": {
            "provider": AZURE_PROVIDER,
            "profiles": {"azure-ja": deepcopy(AZURE_PROFILE_DEFAULTS)},
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
}

TOP_LEVEL_KEYS = {"transcription", "folders", "processing"}
TRANSCRIPTION_KEYS = {"active", LOCAL_MODE, CLOUD_MODE}
ACTIVE_KEYS = {"mode", "profile"}
BACKEND_KEYS = {"provider", "profiles"}
LOCAL_PROFILE_KEYS = set(LOCAL_PROFILE_DEFAULTS)
AZURE_PROFILE_KEYS = {
    "endpoint",
    "region",
    "api_version",
    "locale",
    "diarization",
    "request",
}
V2_TRANSCRIPTION_KEYS = {"active_profile", "profiles"}
V2_LOCAL_PROFILE_KEYS = {"provider", *LOCAL_PROFILE_KEYS}
V2_AZURE_PROFILE_KEYS = {"provider", *AZURE_PROFILE_KEYS}
V2_ALL_PROFILE_KEYS = V2_LOCAL_PROFILE_KEYS | V2_AZURE_PROFILE_KEYS
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
    ("endpoint",): "azure_speech_endpoint",
    ("region",): "azure_speech_region",
    ("api_version",): "azure_speech_api_version",
    ("locale",): "azure_speech_locale",
    ("diarization", "enabled"): "azure_speech_diarization_enabled",
    ("diarization", "max_speakers"): "azure_speech_max_speakers",
    ("request", "timeout_seconds"): "azure_speech_timeout_seconds",
    ("request", "max_retries"): "azure_speech_max_retries",
}

LEGACY_LOCAL_TO_PROFILE = {
    "model": ("model",),
    "language": ("language",),
    "chunk_seconds": ("chunk_seconds",),
    "beam_size": ("beam_size",),
    "compute_type": ("compute_type",),
    "device": ("device",),
}
LEGACY_AZURE_TO_PROFILE = {
    "azure_speech_endpoint": ("endpoint",),
    "azure_speech_region": ("region",),
    "azure_speech_api_version": ("api_version",),
    "azure_speech_locale": ("locale",),
    "azure_speech_diarization_enabled": ("diarization", "enabled"),
    "azure_speech_max_speakers": ("diarization", "max_speakers"),
    "azure_speech_timeout_seconds": ("request", "timeout_seconds"),
    "azure_speech_max_retries": ("request", "max_retries"),
}
LEGACY_FOLDER_TO_V3 = {value: key for key, value in FOLDER_TO_FLAT.items()}
LEGACY_PROCESSING_TO_V3 = {value: key for key, value in PROCESSING_TO_FLAT.items()}


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


type TranscriptionConfig = LocalTranscriptionConfig | AzureSpeechTranscriptionConfig


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
    if type(raw_version) is int and raw_version == 1:
        return {
            "schema_version": raw_version,
            "effective_schema_version": SCHEMA_VERSION,
            "migration": {
                "kind": "legacy_flat_to_execution_modes",
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
    if major == 1:
        if minor > 1:
            raise ConfigError(
                f"Unsupported legacy schema_version {raw_version!r} in {path}; "
                "upgrade the application that created it or convert it manually"
            )
        return {
            "schema_version": raw_version,
            "effective_schema_version": SCHEMA_VERSION,
            "migration": {
                "kind": "legacy_flat_to_execution_modes",
                "from_version": raw_version,
                "to_version": SCHEMA_VERSION,
                "persistent_config_updated": False,
            },
        }
    if major == 2:
        if minor > 0:
            raise ConfigError(
                f"Unsupported legacy schema_version {raw_version!r} in {path}; "
                "upgrade the application that created it or convert it manually"
            )
        return {
            "schema_version": raw_version,
            "effective_schema_version": SCHEMA_VERSION,
            "migration": {
                "kind": "profiles_to_execution_modes",
                "from_version": raw_version,
                "to_version": SCHEMA_VERSION,
                "persistent_config_updated": False,
            },
        }
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
    return {
        "schema_version": raw_version,
        "effective_schema_version": SCHEMA_VERSION,
        "migration": None,
    }


def _schema_major(raw_version: object) -> int:
    if raw_version == 1:
        return 1
    assert isinstance(raw_version, str)
    return int(raw_version.split(".", maxsplit=1)[0])


def _mapping(value: Any, label: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping in {path}")
    return dict(value)


def _reject_unknown(
    value: dict[str, Any], allowed: set[str], label: str, path: Path
) -> None:
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
    if "provider" in profile:
        _require_type(profile["provider"], str, f"{label}.provider", path)
        if profile["provider"] not in PROVIDERS:
            raise ConfigError(
                f"{label}.provider must be either {LOCAL_PROVIDER!r} or "
                f"{AZURE_PROVIDER!r} in {path}"
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


def _validate_named_profiles(
    value: Any,
    *,
    label: str,
    path: Path,
    allowed_keys: set[str],
) -> None:
    profiles = _mapping(value, label, path)
    for raw_name, raw_profile in profiles.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ConfigError(f"transcription profile names must be non-empty strings in {path}")
        profile_label = f"{label}.{raw_name}"
        profile = _mapping(raw_profile, profile_label, path)
        _reject_unknown(profile, allowed_keys, profile_label, path)
        _validate_profile_values(profile, profile_label, path)


def _validate_v2_layer(value: dict[str, Any], path: Path) -> None:
    _reject_unknown(value, TOP_LEVEL_KEYS, "root", path)
    if "transcription" in value:
        transcription = _mapping(value["transcription"], "transcription", path)
        _reject_unknown(transcription, V2_TRANSCRIPTION_KEYS, "transcription", path)
        if "active_profile" in transcription:
            _require_type(
                transcription["active_profile"],
                str,
                "transcription.active_profile",
                path,
            )
            if not transcription["active_profile"].strip():
                raise ConfigError(f"transcription.active_profile must not be empty in {path}")
        if "profiles" in transcription:
            _validate_named_profiles(
                transcription["profiles"],
                label="transcription.profiles",
                path=path,
                allowed_keys=V2_ALL_PROFILE_KEYS,
            )
    _validate_shared_layer(value, path)


def _validate_backend_layer(value: Any, *, mode: str, path: Path) -> None:
    label = f"transcription.{mode}"
    backend = _mapping(value, label, path)
    _reject_unknown(backend, BACKEND_KEYS, label, path)
    expected_provider = LOCAL_PROVIDER if mode == LOCAL_MODE else AZURE_PROVIDER
    if "provider" in backend:
        _require_type(backend["provider"], str, f"{label}.provider", path)
        if backend["provider"] != expected_provider:
            raise ConfigError(
                f"{label}.provider must be {expected_provider!r} in {path}"
            )
    if "profiles" in backend:
        allowed = LOCAL_PROFILE_KEYS if mode == LOCAL_MODE else AZURE_PROFILE_KEYS
        _validate_named_profiles(
            backend["profiles"],
            label=f"{label}.profiles",
            path=path,
            allowed_keys=allowed,
        )


def _validate_v3_layer(value: dict[str, Any], path: Path) -> None:
    _reject_unknown(value, TOP_LEVEL_KEYS, "root", path)
    if "transcription" in value:
        transcription = _mapping(value["transcription"], "transcription", path)
        _reject_unknown(transcription, TRANSCRIPTION_KEYS, "transcription", path)
        if "active" in transcription:
            active = _mapping(transcription["active"], "transcription.active", path)
            _reject_unknown(active, ACTIVE_KEYS, "transcription.active", path)
            if "mode" in active:
                _require_type(active["mode"], str, "transcription.active.mode", path)
                if active["mode"] not in MODES:
                    raise ConfigError(
                        f"transcription.active.mode must be 'local' or 'cloud' in {path}"
                    )
            if "profile" in active:
                _require_type(active["profile"], str, "transcription.active.profile", path)
                if not active["profile"].strip():
                    raise ConfigError(
                        f"transcription.active.profile must not be empty in {path}"
                    )
        for mode in (LOCAL_MODE, CLOUD_MODE):
            if mode in transcription:
                _validate_backend_layer(transcription[mode], mode=mode, path=path)
    _validate_shared_layer(value, path)


def _validate_shared_layer(value: dict[str, Any], path: Path) -> None:
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
            _positive_integer(
                processing["heartbeat_seconds"], "processing.heartbeat_seconds", path
            )
        if "keep_working_files" in processing:
            _require_type(
                processing["keep_working_files"], bool, "processing.keep_working_files", path
            )


def _validate_flat_values(
    values: dict[str, Any], *, require_provider_settings: bool = False
) -> None:
    azure_active = values.get("provider") == AZURE_PROVIDER
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
        raise ConfigError(
            f"provider must be either {LOCAL_PROVIDER!r} or {AZURE_PROVIDER!r}"
        )
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


def _load_yaml(path: Path) -> tuple[dict[str, Any], dict[str, Any], int]:
    _text, loaded = _read_yaml(path)
    schema_report = _inspect_schema_version(loaded, path)
    result = dict(loaded)
    raw_version = result.pop("schema_version")
    schema_major = _schema_major(raw_version)
    if schema_major == 1:
        unknown = sorted(set(result) - set(DEFAULTS))
        if unknown:
            raise ConfigError(f"Unknown config key(s) in {path}: {', '.join(unknown)}")
        candidate = dict(DEFAULTS)
        candidate.update(result)
        try:
            _validate_flat_values(candidate)
        except ConfigError as exc:
            raise ConfigError(f"Invalid config file {path}: {exc}") from exc
    elif schema_major == 2:
        try:
            _validate_v2_layer(result, path)
        except ConfigError as exc:
            raise ConfigError(f"Invalid config file {path}: {exc}") from exc
    else:
        try:
            _validate_v3_layer(result, path)
        except ConfigError as exc:
            raise ConfigError(f"Invalid config file {path}: {exc}") from exc
    return result, schema_report, schema_major


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
        key_path = (*prefix, str(key))
        if isinstance(value, dict):
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = {}
                target[key] = existing
            _merge_config(existing, value, sources, label, key_path)
        else:
            target[key] = deepcopy(value)
            sources[key_path] = label


def _set_nested(target: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    cursor = target
    for key in keys[:-1]:
        child = cursor.setdefault(key, {})
        assert isinstance(child, dict)
        cursor = child
    cursor[keys[-1]] = value


def _mode_for_provider(provider: str) -> str:
    if provider == LOCAL_PROVIDER:
        return LOCAL_MODE
    if provider == AZURE_PROVIDER:
        return CLOUD_MODE
    raise ConfigError(f"Unsupported transcription provider {provider!r}")


def _profile_modes(config: dict[str, Any], profile_name: str) -> list[str]:
    transcription = _mapping(config.get("transcription"), "transcription", Path("<merged>"))
    matches: list[str] = []
    for mode in (LOCAL_MODE, CLOUD_MODE):
        backend = transcription.get(mode)
        if not isinstance(backend, dict):
            continue
        profiles = backend.get("profiles")
        if isinstance(profiles, dict) and profile_name in profiles:
            matches.append(mode)
    return matches


def _apply_v2_layer(
    config: dict[str, Any],
    v2: dict[str, Any],
    sources: dict[tuple[str, ...], str],
    label: str,
) -> None:
    layer: dict[str, Any] = {}
    profile_mode_hints: dict[str, str] = {}
    transcription = v2.get("transcription")
    if isinstance(transcription, dict):
        profiles = transcription.get("profiles")
        if isinstance(profiles, dict):
            for raw_name, raw_profile in profiles.items():
                name = str(raw_name)
                profile = _mapping(raw_profile, f"transcription.profiles.{name}", Path(label))
                converted = dict(profile)
                provider = converted.pop("provider", None)
                if provider is None:
                    matches = _profile_modes(config, name)
                    if len(matches) != 1:
                        raise ConfigError(
                            f"Cannot infer local/cloud mode for schema 2 profile {name!r} "
                            f"in {label}; add provider before migrating"
                        )
                    mode = matches[0]
                else:
                    mode = _mode_for_provider(str(provider))
                    _set_nested(
                        layer,
                        ("transcription", mode, "provider"),
                        provider,
                    )
                profile_mode_hints[name] = mode
                _set_nested(
                    layer,
                    ("transcription", mode, "profiles", name),
                    converted,
                )

        if "active_profile" in transcription:
            active_name = str(transcription["active_profile"])
            active_mode = profile_mode_hints.get(active_name)
            if active_mode is None:
                matches = _profile_modes(config, active_name)
                if len(matches) != 1:
                    raise ConfigError(
                        f"Cannot infer local/cloud mode for schema 2 active profile "
                        f"{active_name!r} in {label}"
                    )
                active_mode = matches[0]
            _set_nested(layer, ("transcription", "active", "mode"), active_mode)
            _set_nested(layer, ("transcription", "active", "profile"), active_name)

    for key in ("folders", "processing"):
        if key in v2:
            layer[key] = deepcopy(v2[key])
    _merge_config(config, layer, sources, label)


def _apply_legacy_layer(
    config: dict[str, Any],
    legacy: dict[str, Any],
    sources: dict[tuple[str, ...], str],
    label: str,
) -> None:
    local_name = "local-small"
    azure_name = "azure-ja"
    layer: dict[str, Any] = {}

    if "provider" in legacy:
        selected_provider = str(legacy["provider"])
        selected_mode = _mode_for_provider(selected_provider)
        selected_name = local_name if selected_mode == LOCAL_MODE else azure_name
        _set_nested(layer, ("transcription", "active", "mode"), selected_mode)
        _set_nested(layer, ("transcription", "active", "profile"), selected_name)
    if any(key in legacy for key in LEGACY_LOCAL_TO_PROFILE):
        for old_key, new_path in LEGACY_LOCAL_TO_PROFILE.items():
            if old_key in legacy:
                _set_nested(
                    layer,
                    ("transcription", LOCAL_MODE, "profiles", local_name, *new_path),
                    legacy[old_key],
                )
    if any(key in legacy for key in LEGACY_AZURE_TO_PROFILE):
        for old_key, azure_path in LEGACY_AZURE_TO_PROFILE.items():
            if old_key in legacy:
                _set_nested(
                    layer,
                    ("transcription", CLOUD_MODE, "profiles", azure_name, *azure_path),
                    legacy[old_key],
                )
    for old_key, new_key in LEGACY_FOLDER_TO_V3.items():
        if old_key in legacy:
            _set_nested(layer, ("folders", new_key), legacy[old_key])
    for old_key, processing_path in LEGACY_PROCESSING_TO_V3.items():
        if old_key in legacy:
            _set_nested(layer, ("processing", *processing_path), legacy[old_key])
    _merge_config(config, layer, sources, label)


def _effective_profile(
    config: dict[str, Any], mode: str, name: str, path: Path
) -> tuple[str, dict[str, Any]]:
    transcription = _mapping(config.get("transcription"), "transcription", path)
    backend = _mapping(transcription.get(mode), f"transcription.{mode}", path)
    provider = backend.get("provider")
    expected_provider = LOCAL_PROVIDER if mode == LOCAL_MODE else AZURE_PROVIDER
    if provider != expected_provider:
        raise ConfigError(
            f"transcription.{mode}.provider is required and must be "
            f"{expected_provider!r} in {path}"
        )
    profiles = _mapping(backend.get("profiles"), f"transcription.{mode}.profiles", path)
    if name not in profiles:
        available = ", ".join(sorted(str(item) for item in profiles)) or "(none)"
        raise ConfigError(
            f"Unknown transcription profile {mode}/{name!s}; available {mode} "
            f"profiles: {available}"
        )
    profile = _mapping(profiles[name], f"transcription.{mode}.profiles.{name}", path)
    allowed = LOCAL_PROFILE_KEYS if mode == LOCAL_MODE else AZURE_PROFILE_KEYS
    _reject_unknown(profile, allowed, f"transcription.{mode}.profiles.{name}", path)
    effective = deepcopy(PROFILE_DEFAULTS[mode])
    throwaway_sources: dict[tuple[str, ...], str] = {}
    _merge_config(effective, profile, throwaway_sources, "profile")
    _validate_profile_values(effective, f"transcription.{mode}.profiles.{name}", path)
    return str(provider), effective


def _get_nested(value: dict[str, Any], keys: tuple[str, ...]) -> Any:
    cursor: Any = value
    for key in keys:
        cursor = cursor[key]
    return cursor


def _select_profile(
    config: dict[str, Any], selector: str | None, path: Path
) -> tuple[str, str]:
    transcription = _mapping(config.get("transcription"), "transcription", path)
    if selector is None:
        active = _mapping(transcription.get("active"), "transcription.active", path)
        return str(active.get("mode")), str(active.get("profile"))

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
        raise ConfigError(
            f"Ambiguous transcription profile {selector!r}; use one of: {choices}"
        )
    return matches[0], selector


def _normalized_values(
    config: dict[str, Any],
    source_paths: dict[tuple[str, ...], str],
    mode: str,
    profile_name: str,
    validation_path: Path,
) -> tuple[dict[str, Any], dict[str, str], tuple[dict[str, Any], ...]]:
    transcription = _mapping(config.get("transcription"), "transcription", validation_path)
    summaries: list[dict[str, Any]] = []
    for candidate_mode in (LOCAL_MODE, CLOUD_MODE):
        backend = _mapping(
            transcription.get(candidate_mode),
            f"transcription.{candidate_mode}",
            validation_path,
        )
        profiles = _mapping(
            backend.get("profiles"),
            f"transcription.{candidate_mode}.profiles",
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
    values = dict(DEFAULTS)
    value_sources = {key: "built-in default" for key in values}
    values["provider"] = provider
    value_sources["provider"] = source_paths.get(
        ("transcription", mode, "provider"), "built-in default"
    )
    if mode == LOCAL_MODE:
        for profile_key, flat_key in LOCAL_PROFILE_TO_FLAT.items():
            values[flat_key] = profile[profile_key]
            value_sources[flat_key] = source_paths.get(
                ("transcription", mode, "profiles", profile_name, profile_key),
                "built-in default",
            )
    else:
        for profile_path, flat_key in AZURE_PROFILE_TO_FLAT.items():
            values[flat_key] = _get_nested(profile, profile_path)
            value_sources[flat_key] = source_paths.get(
                ("transcription", mode, "profiles", profile_name, *profile_path),
                "built-in default",
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
    )


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
) -> ResolvedConfig:
    current_directory = cwd.resolve()
    home_directory = (home or Path.home()).resolve()
    config = deepcopy(BUILT_IN_CONFIG)
    source_paths: dict[tuple[str, ...], str] = {}
    _record_sources(config, source_paths, "built-in default")
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
        layer, schema_report, schema_major = _load_yaml(path)
        source_report.update(schema_report)
        source_label = f"{label}: {path}"
        if schema_major == 1:
            _apply_legacy_layer(config, layer, source_paths, source_label)
        elif schema_major == 2:
            _apply_v2_layer(config, layer, source_paths, source_label)
        else:
            _merge_config(config, layer, source_paths, source_label)
        loaded_files.append(path)

    selected_mode, selected_profile = _select_profile(
        config, profile, current_directory
    )
    active_mode_source = (
        "CLI option"
        if profile is not None
        else source_paths.get(("transcription", "active", "mode"), "built-in default")
    )
    active_profile_source = (
        "CLI option"
        if profile is not None
        else source_paths.get(("transcription", "active", "profile"), "built-in default")
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


def _config_text(config: dict[str, Any]) -> str:
    if config == BUILT_IN_CONFIG:
        return config_example_text()
    body = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    return f'schema_version: "{SCHEMA_VERSION}"\n\n{body}'


def migrate_config_file(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    target = path.expanduser().resolve(strict=False)
    if not target.exists():
        raise ConfigError(f"Config file not found: {target}")
    if not target.is_file():
        raise ConfigError(f"Config path is not a file: {target}")

    source, schema_report, schema_major = _load_yaml(target)
    migration = schema_report["migration"]
    if migration is None:
        return {
            "status": "unchanged",
            "config_path": str(target),
            "schema_version": schema_report["schema_version"],
            "effective_schema_version": SCHEMA_VERSION,
            "backup_path": None,
        }

    migrated = deepcopy(BUILT_IN_CONFIG)
    migrated_sources: dict[tuple[str, ...], str] = {}
    _record_sources(migrated, migrated_sources, "built-in default")
    source_label = f"legacy config: {target}"
    if schema_major == 1:
        _apply_legacy_layer(migrated, source, migrated_sources, source_label)
    else:
        _apply_v2_layer(migrated, source, migrated_sources, source_label)
    migrated_text = _config_text(migrated)

    try:
        parsed = yaml.safe_load(migrated_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Migrated config is not valid YAML: {target}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"Migrated config root must be a mapping: {target}")
    parsed_version = parsed.pop("schema_version", None)
    if parsed_version != SCHEMA_VERSION:
        raise ConfigError(f"Config migration did not reach schema {SCHEMA_VERSION}: {target}")
    _validate_v3_layer(parsed, target)
    active_mode, active_profile = _select_profile(migrated, None, target)
    migrated_values, _migrated_value_sources, _migrated_profiles = _normalized_values(
        migrated, migrated_sources, active_mode, active_profile, target
    )
    _validate_flat_values(migrated_values, require_provider_settings=True)

    backup_path = _next_backup_path(target)
    if dry_run:
        return {
            "status": "planned",
            "config_path": str(target),
            "from_schema_version": schema_report["schema_version"],
            "to_schema_version": SCHEMA_VERSION,
            "active_mode": active_mode,
            "active_profile": active_profile,
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
        "active_mode": active_mode,
        "active_profile": active_profile,
        "backup_path": str(backup_path),
    }

from pathlib import Path

import pytest
import yaml

from audio_transcriber.config import (
    AzureSpeechTranscriptionConfig,
    ConfigError,
    ResolvedConfig,
    config_example_text,
    resolve_config,
)
from audio_transcriber.pipeline import _fingerprint


def resolve_cloud(tmp_path: Path, **overrides: object) -> ResolvedConfig:
    return resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        profile="cloud/azure-ja",
        cli_overrides={
            "azure_speech_endpoint": "https://example.cognitiveservices.azure.com/",
            "state_dir": str(tmp_path / "state"),
            **overrides,
        },
    )


def test_explicit_example_defaults_and_effective_defaults(tmp_path: Path) -> None:
    profile = yaml.safe_load(config_example_text())["transcription"]["cloud"]["profiles"][
        "azure-ja"
    ]
    assert profile["diarization"] == {"enabled": False, "max_speakers": 8}
    assert profile["profanity_filter_mode"] == "None"
    assert profile["phrase_list"] == {"phrases": []}
    assert profile["authentication"] == {"reuse_cached_credentials": True}
    config = resolve_cloud(tmp_path).transcription
    assert isinstance(config, AzureSpeechTranscriptionConfig)
    assert config.diarization_enabled is False
    assert config.profanity_filter_mode == "None"
    assert config.phrases == ()
    assert config.reuse_cached_credentials is True
    assert config.authentication_dir == tmp_path / "state" / "auth"
    assert not config.authentication_dir.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"azure_speech_phrases": "word"},
        {"azure_speech_phrases": [""]},
        {"azure_speech_phrases": [" "]},
        {"azure_speech_phrases": [1]},
        {"azure_speech_phrases": ["word"] * 501},
        {"azure_speech_profanity_filter_mode": "invalid"},
        {"azure_speech_reuse_cached_credentials": "yes"},
        {"azure_speech_phrases": ["word"], "azure_speech_api_version": "2024-11-15"},
    ],
)
def test_invalid_settings_rejected(tmp_path: Path, override: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        resolve_cloud(tmp_path, **override)


@pytest.mark.parametrize(
    "profile_settings",
    [
        {"phrase_list": {"phrases": [False]}},
        {"phrase_list": {"unknown": []}},
        {"profanity_filter_mode": None},
        {"authentication": {"reuse_cached_credentials": "yes"}},
    ],
)
def test_invalid_yaml_settings(tmp_path: Path, profile_settings: dict[str, object]) -> None:
    path = tmp_path / ".tkn" / "config.yaml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0.0",
                "transcription": {"cloud": {"profiles": {"azure-ja": profile_settings}}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        resolve_cloud(tmp_path)


def test_phrase_list_yaml_replaces_list_and_affects_fingerprint(tmp_path: Path) -> None:
    baseline = resolve_cloud(tmp_path)
    path = tmp_path / ".tkn" / "config.yaml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0.0",
                "transcription": {
                    "cloud": {
                        "profiles": {
                            "azure-ja": {
                                "phrase_list": {"phrases": ["Contoso", "用語"]},
                                "profanity_filter_mode": "Masked",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    configured = resolve_cloud(tmp_path)
    assert isinstance(configured.transcription, AzureSpeechTranscriptionConfig)
    assert configured.transcription.phrases == ("Contoso", "用語")
    assert _fingerprint("hash", 1, configured) != _fingerprint("hash", 1, baseline)
    assert _fingerprint(
        "hash",
        1,
        resolve_cloud(
            tmp_path,
            azure_speech_phrases=[],
            azure_speech_profanity_filter_mode="None",
        ),
    ) == _fingerprint("hash", 1, baseline)
    assert _fingerprint(
        "hash",
        1,
        resolve_cloud(
            tmp_path,
            azure_speech_reuse_cached_credentials=False,
        ),
    ) == _fingerprint("hash", 1, configured)

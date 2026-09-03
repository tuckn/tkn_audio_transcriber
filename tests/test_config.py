from pathlib import Path

import pytest
import yaml

from audio_transcriber.config import (
    SCHEMA_VERSION,
    AzureSpeechTranscriptionConfig,
    ConfigError,
    LocalTranscriptionConfig,
    config_example_text,
    initialize_user_config,
    migrate_config_file,
    resolve_config,
)


def test_config_precedence_deep_merge_sources_and_schema_reports(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit.yaml"
    (home / ".tkn" / "audio_transcriber").mkdir(parents=True)
    (cwd / ".tkn").mkdir(parents=True)
    (home / ".tkn" / "audio_transcriber" / "config.yaml").write_text(
        'schema_version: "3.0.0"\n'
        "transcription:\n"
        "  local:\n"
        "    profiles:\n"
        "      local-small:\n"
        "        language: en\n"
        "        chunk_seconds: 100\n",
        encoding="utf-8",
    )
    (cwd / ".tkn" / "config.yaml").write_text(
        'schema_version: "3.0.0"\n'
        "transcription:\n"
        "  local:\n"
        "    profiles:\n"
        "      local-small:\n"
        "        chunk_seconds: 200\n"
        "        beam_size: 2\n",
        encoding="utf-8",
    )
    explicit.write_text(
        'schema_version: "3.0.7"\n'
        "transcription:\n"
        "  local:\n"
        "    profiles:\n"
        "      local-small:\n"
        "        beam_size: 3\n"
        "        model: medium\n",
        encoding="utf-8",
    )

    resolved = resolve_config(
        cwd=cwd,
        home=home,
        explicit_config=explicit,
        cli_overrides={"model": "small", "device": "cuda"},
    )

    assert resolved.values["language"] == "en"
    assert resolved.values["chunk_seconds"] == 200
    assert resolved.values["beam_size"] == 3
    assert resolved.values["model"] == "small"
    assert resolved.values["device"] == "cuda"
    assert resolved.sources["language"].startswith("user config:")
    assert resolved.sources["chunk_seconds"].startswith("working-directory config:")
    assert resolved.sources["beam_size"].startswith("explicit config:")
    assert resolved.sources["model"] == "CLI option"
    assert resolved.sources["compute_type"] == "built-in default"
    assert "schema_version" not in resolved.sources
    assert [source["kind"] for source in resolved.config_sources] == [
        "built_in",
        "user",
        "working_directory",
        "explicit",
    ]
    assert resolved.config_sources[-1]["schema_version"] == "3.0.7"
    assert all(
        source["effective_schema_version"] == SCHEMA_VERSION
        for source in resolved.config_sources
    )
    assert not resolved.has_in_memory_migrations


def test_named_profiles_can_select_multiple_local_models(tmp_path: Path) -> None:
    default = resolve_config(cwd=tmp_path, home=tmp_path / "home")
    large = resolve_config(
        cwd=tmp_path, home=tmp_path / "home", profile="local/local-large"
    )

    assert default.active_mode == "local"
    assert default.active_profile == "local-small"
    assert isinstance(default.transcription, LocalTranscriptionConfig)
    assert default.values["model"] == "small"
    assert large.active_profile == "local-large"
    assert large.active_profile_source == "CLI option"
    assert large.values["provider"] == "faster-whisper"
    assert large.values["model"] == "large-v3"


def test_custom_profile_inherits_provider_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'schema_version: "3.0.0"\n'
        "transcription:\n"
        "  active:\n"
        "    mode: local\n"
        "    profile: local-gpu\n"
        "  local:\n"
        "    profiles:\n"
        "      local-gpu:\n"
        "        model: medium\n"
        "        device: cuda\n",
        encoding="utf-8",
    )

    resolved = resolve_config(
        cwd=tmp_path, home=tmp_path / "home", explicit_config=config
    )

    assert resolved.active_profile == "local-gpu"
    assert resolved.values["model"] == "medium"
    assert resolved.values["device"] == "cuda"
    assert resolved.values["language"] == "ja"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("folders:\n  output: .\n", "schema_version is required"),
        ("schema_version: 99\n", "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "2"\n', "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "2.0"\n', "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "2.0.0-rc1"\n', "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "0.9.0"\n', "Unsupported older schema_version"),
        ('schema_version: "2.1.0"\n', "Unsupported legacy schema_version"),
        ('schema_version: "4.0.0"\n', "Unsupported newer schema_version"),
        ('schema_version: "3.0.0"\nunknown_key: value\n', "Unknown config key"),
        (
            'schema_version: "3.0.0"\n'
            "transcription:\n"
            "  local:\n"
            "    profiles:\n"
            "      local-small:\n"
            "        chunk_seconds: false\n",
            "must be an integer",
        ),
        (
            'schema_version: "3.0.0"\nprocessing:\n  heartbeat_seconds: 0\n',
            "greater than zero",
        ),
        (
            'schema_version: "3.0.0"\n'
            "transcription:\n"
            "  active:\n"
            "    mode: local\n"
            "    profile: missing\n",
            "Unknown transcription profile",
        ),
        (
            'schema_version: "3.0.0"\n'
            "transcription:\n"
            "  local:\n"
            "    profiles:\n"
            "      invalid:\n"
            "        endpoint: https://example.invalid\n",
            "Unknown config key",
        ),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, content: str, message: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)


def test_each_config_source_is_validated_before_merge(tmp_path: Path) -> None:
    home_config = tmp_path / "home" / ".tkn" / "audio_transcriber" / "config.yaml"
    home_config.parent.mkdir(parents=True)
    home_config.write_text(
        'schema_version: "3.0.0"\nprocessing:\n  heartbeat_seconds: false\n',
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(
        'schema_version: "3.0.0"\nprocessing:\n  heartbeat_seconds: 60\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must be an integer"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            explicit_config=explicit,
        )


def test_legacy_integer_schema_is_structurally_migrated_in_memory(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")

    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)

    assert resolved.has_in_memory_migrations
    assert resolved.config_sources[-1]["migration"] == {
        "kind": "legacy_flat_to_execution_modes",
        "from_version": 1,
        "to_version": SCHEMA_VERSION,
        "persistent_config_updated": False,
    }
    assert resolved.values["language"] == "en"


def test_schema_1_1_remains_readable_and_migratable(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'schema_version: "1.1.0"\nprovider: faster-whisper\nmodel: medium\n',
        encoding="utf-8",
    )

    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)
    planned = migrate_config_file(config, dry_run=True)

    assert resolved.has_in_memory_migrations
    assert resolved.values["provider"] == "faster-whisper"
    assert resolved.values["model"] == "medium"
    assert planned["from_schema_version"] == "1.1.0"
    assert planned["to_schema_version"] == "3.0.0"
    assert planned["active_mode"] == "local"
    assert planned["active_profile"] == "local-small"


def test_schema_2_profiles_are_grouped_in_memory_and_migratable(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'schema_version: "2.0.0"\n'
        "transcription:\n"
        "  active_profile: azure-custom\n"
        "  profiles:\n"
        "    local-custom:\n"
        "      provider: faster-whisper\n"
        "      model: medium\n"
        "    azure-custom:\n"
        "      provider: azure-speech-fast\n"
        "      endpoint: https://example.cognitiveservices.azure.com/\n",
        encoding="utf-8",
    )

    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)
    planned = migrate_config_file(config, dry_run=True)

    assert resolved.has_in_memory_migrations
    assert resolved.active_mode == "cloud"
    assert resolved.active_profile == "azure-custom"
    assert resolved.transcription.mode == "cloud"
    assert isinstance(resolved.transcription, AzureSpeechTranscriptionConfig)
    assert planned["from_schema_version"] == "2.0.0"
    assert planned["to_schema_version"] == "3.0.0"
    assert planned["active_mode"] == "cloud"

    migrated = migrate_config_file(config)
    migrated_yaml = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert migrated["status"] == "migrated"
    assert migrated_yaml["schema_version"] == "3.0.0"
    assert migrated_yaml["transcription"]["active"] == {
        "mode": "cloud",
        "profile": "azure-custom",
    }
    assert migrated_yaml["transcription"]["local"]["profiles"]["local-custom"][
        "model"
    ] == "medium"
    assert migrated_yaml["transcription"]["cloud"]["profiles"]["azure-custom"][
        "endpoint"
    ] == "https://example.cognitiveservices.azure.com/"


def test_provider_cannot_be_overridden_independently(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be overridden independently"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            cli_overrides={"provider": "azure-speech-fast"},
        )


def test_provider_specific_cli_options_must_match_selected_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cloud-only option"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            cli_overrides={
                "azure_speech_endpoint": "https://example.cognitiveservices.azure.com/"
            },
        )
    with pytest.raises(ConfigError, match="local-only option"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            profile="cloud/azure-ja",
            cli_overrides={
                "azure_speech_endpoint": "https://example.cognitiveservices.azure.com/",
                "model": "medium",
            },
        )


def test_schema_version_cannot_be_a_cli_override(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be a CLI override"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            cli_overrides={"schema_version": "3.0.0"},
        )


def test_relative_paths_are_resolved_from_current_working_directory(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'schema_version: "3.0.0"\nfolders:\n  output: relative/output\n',
        encoding="utf-8",
    )
    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)
    assert resolved.path("output_dir") == (tmp_path / "relative" / "output").resolve()


def test_output_dir_defaults_to_current_working_directory(tmp_path: Path) -> None:
    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home")

    assert resolved.path("output_dir") == tmp_path.resolve()
    assert resolved.sources["output_dir"] == "built-in default"


def test_packaged_example_has_hierarchical_complete_schema(tmp_path: Path) -> None:
    text = config_example_text()
    loaded = yaml.safe_load(text)

    assert text.splitlines()[0] == f'schema_version: "{SCHEMA_VERSION}"'
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert set(loaded) == {"schema_version", "transcription", "folders", "processing"}
    assert loaded["transcription"]["active"] == {
        "mode": "local",
        "profile": "local-small",
    }
    assert set(loaded["transcription"]["local"]["profiles"]) == {
        "local-small",
        "local-large",
        "gpu-quality",
        "gpu-fast",
    }
    assert set(loaded["transcription"]["cloud"]["profiles"]) == {"azure-ja"}
    assert resolve_config(cwd=tmp_path, home=tmp_path / "home").values["model"] == "small"


def test_config_init_is_idempotent_and_protects_edited_config(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"

    planned = initialize_user_config(target, dry_run=True)
    assert planned["status"] == "planned"
    assert not target.exists()

    created = initialize_user_config(target)
    unchanged = initialize_user_config(target)
    target.write_text("schema_version: 1\nmodel: edited\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="different content"):
        initialize_user_config(target)
    replaced = initialize_user_config(target, force=True)

    assert created["status"] == "created"
    assert unchanged["status"] == "unchanged"
    assert replaced["status"] == "replaced"
    assert target.read_text(encoding="utf-8") == config_example_text()
    backup = Path(str(replaced["backup_path"]))
    assert backup.read_text(encoding="utf-8") == "schema_version: 1\nmodel: edited\n"


def test_config_migrate_restructures_with_dry_run_backup_and_atomic_update(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.yaml"
    original = (
        'schema_version: "1.1.0"\n'
        "provider: faster-whisper\n"
        "model: medium\n"
        "language: en\n"
        "output_dir: relative/output\n"
    )
    target.write_text(original, encoding="utf-8")

    planned = migrate_config_file(target, dry_run=True)
    assert planned["status"] == "planned"
    assert target.read_text(encoding="utf-8") == original
    assert not Path(str(planned["backup_path"])).exists()

    migrated = migrate_config_file(target)
    migrated_yaml = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert migrated["status"] == "migrated"
    assert migrated_yaml["schema_version"] == "3.0.0"
    assert migrated_yaml["transcription"]["active"] == {
        "mode": "local",
        "profile": "local-small",
    }
    assert migrated_yaml["transcription"]["local"]["profiles"]["local-small"][
        "model"
    ] == "medium"
    assert migrated_yaml["transcription"]["local"]["profiles"]["local-small"][
        "language"
    ] == "en"
    assert migrated_yaml["folders"]["output"] == "relative/output"
    backup = Path(str(migrated["backup_path"]))
    assert backup.read_text(encoding="utf-8") == original
    assert not resolve_config(
        cwd=tmp_path, home=tmp_path / "home", explicit_config=target
    ).has_in_memory_migrations


def test_config_migrate_leaves_current_schema_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    text = 'schema_version: "3.0.3"\nfolders:\n  output: .\n'
    target.write_text(text, encoding="utf-8")

    result = migrate_config_file(target)

    assert result["status"] == "unchanged"
    assert result["schema_version"] == "3.0.3"
    assert target.read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("*.bak*"))


def test_azure_profile_requires_valid_endpoint_only_when_selected(tmp_path: Path) -> None:
    local = resolve_config(cwd=tmp_path, home=tmp_path / "home")
    assert local.values["provider"] == "faster-whisper"

    with pytest.raises(ConfigError, match="azure_speech_endpoint is required"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", profile="cloud/azure-ja")

    azure = tmp_path / "azure.yaml"
    azure.write_text(
        'schema_version: "3.0.0"\n'
        "transcription:\n"
        "  cloud:\n"
        "    profiles:\n"
        "      azure-ja:\n"
        "        endpoint: https://example.cognitiveservices.azure.com/\n",
        encoding="utf-8",
    )
    resolved = resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        explicit_config=azure,
        profile="cloud/azure-ja",
    )
    assert resolved.values["provider"] == "azure-speech-fast"
    assert resolved.values["azure_speech_api_version"] == "2025-10-15"


def test_cloud_upload_approval_cannot_be_saved_in_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'schema_version: "3.0.0"\n'
        "transcription:\n"
        "  allow_cloud_upload: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Unknown config key"):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)

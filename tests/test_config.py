from pathlib import Path

import pytest
import yaml

from audio_transcriber.config import (
    SCHEMA_VERSION,
    ConfigError,
    config_example_text,
    initialize_user_config,
    migrate_config_file,
    resolve_config,
)


def test_config_precedence_sources_and_schema_reports(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit.yaml"
    (home / ".tkn" / "audio_transcriber").mkdir(parents=True)
    (cwd / ".tkn").mkdir(parents=True)
    (home / ".tkn" / "audio_transcriber" / "config.yaml").write_text(
        'schema_version: "1.1.0"\nlanguage: en\nchunk_seconds: 100\n',
        encoding="utf-8",
    )
    (cwd / ".tkn" / "config.yaml").write_text(
        'schema_version: "1.1.0"\nchunk_seconds: 200\nbeam_size: 2\n',
        encoding="utf-8",
    )
    explicit.write_text(
        'schema_version: "1.1.7"\nbeam_size: 3\nmodel: medium\n',
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
    assert resolved.config_sources[-1]["schema_version"] == "1.1.7"
    assert all(
        source["effective_schema_version"] == SCHEMA_VERSION for source in resolved.config_sources
    )
    assert not resolved.has_in_memory_migrations


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("language: en\n", "schema_version is required"),
        ("schema_version: 99\n", "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "1"\n', "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "1.0"\n', "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "1.0.0-rc1"\n', "expected a quoted MAJOR.MINOR.PATCH"),
        ('schema_version: "0.9.0"\n', "Unsupported older schema_version"),
        ('schema_version: "1.2.0"\n', "Unsupported newer schema_version"),
        ('schema_version: "2.0.0"\n', "Unsupported newer schema_version"),
        ('schema_version: "1.0.0"\nunknown_key: value\n', "Unknown config key"),
        ('schema_version: "1.0.0"\nchunk_seconds: false\n', "must be an integer"),
        ('schema_version: "1.0.0"\nchunk_seconds: 0\n', "greater than zero"),
        ('schema_version: "1.0.0"\nheartbeat_seconds: 0\n', "greater than zero"),
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
    home_config.write_text('schema_version: "1.0.0"\nchunk_seconds: false\n', encoding="utf-8")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text('schema_version: "1.0.0"\nchunk_seconds: 600\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="must be an integer"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            explicit_config=explicit,
        )


def test_legacy_integer_schema_is_migrated_in_memory(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("schema_version: 1\nlanguage: en\n", encoding="utf-8")

    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)

    assert resolved.has_in_memory_migrations
    assert resolved.config_sources[-1]["migration"] == {
        "kind": "legacy_integer_version",
        "from_version": 1,
        "to_version": SCHEMA_VERSION,
        "persistent_config_updated": False,
    }
    assert resolved.values["language"] == "en"


def test_schema_1_0_remains_readable_and_migratable(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text('schema_version: "1.0.7"\nlanguage: en\n', encoding="utf-8")

    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)
    planned = migrate_config_file(config, dry_run=True)

    assert resolved.has_in_memory_migrations
    assert resolved.values["provider"] == "faster-whisper"
    assert planned["from_schema_version"] == "1.0.7"
    assert planned["to_schema_version"] == "1.1.0"


def test_schema_version_cannot_be_a_cli_override(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot be a CLI override"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            cli_overrides={"schema_version": "1.0.0"},
        )


def test_relative_paths_are_resolved_from_current_working_directory(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text('schema_version: "1.0.0"\noutput_dir: relative/output\n', encoding="utf-8")
    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)
    assert resolved.path("output_dir") == (tmp_path / "relative" / "output").resolve()


def test_output_dir_defaults_to_current_working_directory(tmp_path: Path) -> None:
    resolved = resolve_config(cwd=tmp_path, home=tmp_path / "home")

    assert resolved.path("output_dir") == tmp_path.resolve()
    assert resolved.sources["output_dir"] == "built-in default"


def test_packaged_example_has_quoted_schema_version_first(tmp_path: Path) -> None:
    text = config_example_text()
    loaded = yaml.safe_load(text)
    default_keys = resolve_config(cwd=tmp_path, home=tmp_path / "home").values

    assert text.splitlines()[0] == f'schema_version: "{SCHEMA_VERSION}"'
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert set(loaded) == {"schema_version", *default_keys}


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


def test_config_migrate_supports_dry_run_backup_and_atomic_update(
    tmp_path: Path,
) -> None:
    target = tmp_path / "config.yaml"
    original = "# personal setting\nschema_version: 1  # legacy\nlanguage: en\n"
    target.write_text(original, encoding="utf-8")

    planned = migrate_config_file(target, dry_run=True)
    assert planned["status"] == "planned"
    assert target.read_text(encoding="utf-8") == original
    assert not Path(str(planned["backup_path"])).exists()

    migrated = migrate_config_file(target)
    assert migrated["status"] == "migrated"
    assert target.read_text(encoding="utf-8") == (
        '# personal setting\nschema_version: "1.1.0"  # legacy\nlanguage: en\n'
    )
    backup = Path(str(migrated["backup_path"]))
    assert backup.read_text(encoding="utf-8") == original
    assert not resolve_config(
        cwd=tmp_path, home=tmp_path / "home", explicit_config=target
    ).has_in_memory_migrations


def test_config_migrate_leaves_current_schema_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    text = 'schema_version: "1.1.3"\nlanguage: en\n'
    target.write_text(text, encoding="utf-8")

    result = migrate_config_file(target)

    assert result["status"] == "unchanged"
    assert result["schema_version"] == "1.1.3"
    assert target.read_text(encoding="utf-8") == text
    assert not list(tmp_path.glob("*.bak*"))


def test_azure_provider_requires_valid_endpoint_only_when_selected(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text(
        'schema_version: "1.1.0"\nazure_speech_endpoint: 123\n',
        encoding="utf-8",
    )
    resolved = resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        explicit_config=local,
    )
    assert resolved.values["provider"] == "faster-whisper"

    azure = tmp_path / "azure.yaml"
    azure.write_text(
        'schema_version: "1.1.0"\nprovider: azure-speech-fast\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="azure_speech_endpoint is required"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            explicit_config=azure,
        )

    azure.write_text(
        'schema_version: "1.1.0"\n'
        'provider: azure-speech-fast\n'
        'azure_speech_endpoint: https://example.cognitiveservices.azure.com/\n',
        encoding="utf-8",
    )
    resolved = resolve_config(
        cwd=tmp_path,
        home=tmp_path / "home",
        explicit_config=azure,
    )
    assert resolved.values["azure_speech_api_version"] == "2025-10-15"


def test_cloud_upload_approval_cannot_be_saved_in_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'schema_version: "1.1.0"\nallow_cloud_upload: true\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Unknown config key"):
        resolve_config(
            cwd=tmp_path,
            home=tmp_path / "home",
            explicit_config=config,
        )

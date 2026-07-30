from pathlib import Path

import pytest

from audio_transcriber.config import ConfigError, resolve_config


def test_config_precedence_and_sources(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit.yaml"
    (home / ".tkn" / "audio_transcriber").mkdir(parents=True)
    (cwd / ".tkn").mkdir(parents=True)
    (home / ".tkn" / "audio_transcriber" / "config.yaml").write_text(
        "schema_version: 1\nlanguage: en\nchunk_seconds: 100\n", encoding="utf-8"
    )
    (cwd / ".tkn" / "config.yaml").write_text(
        "schema_version: 1\nchunk_seconds: 200\nbeam_size: 2\n", encoding="utf-8"
    )
    explicit.write_text(
        "schema_version: 1\nbeam_size: 3\nmodel: medium\n", encoding="utf-8"
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


@pytest.mark.parametrize(
    "content",
    [
        "schema_version: 99\n",
        "schema_version: 1\nunknown_key: value\n",
        "schema_version: 1\nchunk_seconds: false\n",
        "schema_version: 1\nchunk_seconds: 0\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        resolve_config(cwd=tmp_path, home=tmp_path / "home", explicit_config=config)


def test_relative_paths_are_resolved_from_current_working_directory(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "schema_version: 1\noutput_dir: relative/output\n", encoding="utf-8"
    )
    resolved = resolve_config(
        cwd=tmp_path, home=tmp_path / "home", explicit_config=config
    )
    assert resolved.path("output_dir") == (tmp_path / "relative" / "output").resolve()


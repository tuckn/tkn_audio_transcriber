from pathlib import Path

import pytest
import yaml

from audio_transcriber.config import _config_text, _read_yaml
from audio_transcriber.yaml_format import StringDateLoader, dump_yaml


@pytest.mark.parametrize("value", [
    "2026-10-07", "2026-10-07T01:02:03Z",
    "2026-10-07T01:02:03.123456789+09:00", "2026-10-07T01:02:03-05:30",
])
@pytest.mark.parametrize("quote", ["", "'", '\"'])
def test_dates_keep_spelling_and_precision(value: str, quote: str, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"custom: {quote}{value}{quote}\n", encoding="utf-8")
    _, data = _read_yaml(path)
    text = dump_yaml({"custom": data["custom"], "dates": [data["custom"]]})
    assert f'custom: "{value}"' in text
    assert f'- "{value}"' in text
    assert yaml.safe_load(text) == {"custom": value, "dates": [value]}
    assert dump_yaml(yaml.safe_load(text)) == text


@pytest.mark.parametrize("value", [
    r"C:\path\to\input", "C:/path/to/input", r"\\server\share\input",
    r"C:\path\user's input", r".\input", r"relative\input",
])
def test_windows_paths(value: str) -> None:
    text = _config_text({"folders": {"output": value}})
    escaped = value.replace("'", "''")
    assert f"output: '{escaped}'" in text
    assert yaml.safe_load(text)["folders"]["output"] == value


@pytest.mark.parametrize("value", ["file:///C:/path/to/input", "https://example.com/2026-10-07"])
def test_urls_are_not_windows_paths(value: str) -> None:
    assert dump_yaml({"source": value}) == f"source: {value}\n"


def test_types_and_default_loader_are_unchanged() -> None:
    value = {"empty": "", "none": None, "list": [], "flag": True, "count": 3}
    assert yaml.safe_load(dump_yaml(value)) == value
    assert not isinstance(yaml.safe_load("2026-10-07"), str)
    assert yaml.load("2026-10-07", Loader=StringDateLoader) == "2026-10-07"

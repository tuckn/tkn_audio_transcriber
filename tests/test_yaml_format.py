from pathlib import Path

import pytest
import yaml

from audio_transcriber.config import _read_yaml
from audio_transcriber.yaml_format import StringDateLoader


@pytest.mark.parametrize("value", [
    "2026-10-07", "2026-10-07T01:02:03Z",
    "2026-10-07T01:02:03.123456789+09:00", "2026-10-07T01:02:03-05:30",
])
@pytest.mark.parametrize("quote", ["", "'", '\"'])
def test_dates_keep_spelling_and_precision(value: str, quote: str, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"custom: {quote}{value}{quote}\n", encoding="utf-8")
    _, data = _read_yaml(path)
    assert data == {"custom": value}


def test_default_loader_is_unchanged() -> None:
    assert not isinstance(yaml.safe_load("2026-10-07"), str)
    assert yaml.load("2026-10-07", Loader=StringDateLoader) == "2026-10-07"

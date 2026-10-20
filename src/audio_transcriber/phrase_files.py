"""Read human-editable vocabulary without changing the source files."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .errors import ConfigError

MAX_PHRASES = 500
MAX_PHRASE_FILE_BYTES = 1_000_000


def validate_phrase(phrase: object) -> str:
    if not isinstance(phrase, str) or not phrase.strip():
        raise ConfigError("Phrases must be non-empty strings")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in phrase):
        raise ConfigError("Phrases must be single-line strings without control characters")
    return phrase.strip()


def read_phrase_files(
    inline: list[str], paths: list[Path]
) -> tuple[list[str], tuple[dict[str, Any], ...]]:
    phrases = [validate_phrase(item) for item in inline]
    records: list[dict[str, Any]] = []
    for path in dict.fromkeys(paths):
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_PHRASE_FILE_BYTES + 1)
            if len(content) > MAX_PHRASE_FILE_BYTES:
                raise ConfigError("Phrase file exceeds the 1 MB limit")
            text = content.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"Cannot read UTF-8 phrase file: {path}") from exc
        items = [
            validate_phrase(line)
            for line in (raw.removesuffix("\r") for raw in text.split("\n"))
            if line.strip() and not line.lstrip().startswith("#")
        ]
        phrases.extend(items)
        records.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "phrase_count": len(items),
            }
        )
    # Preserve spelling, case and first appearance; do not conflate distinct names.
    phrases = list(dict.fromkeys(phrases))
    if len(phrases) > MAX_PHRASES:
        raise ConfigError(f"Combined phrase list exceeds {MAX_PHRASES} unique phrases")
    return phrases, tuple(records)

"""CLI-only rendering; never modifies application results or saved records."""

from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any


def text_value(value: Any) -> str:
    if isinstance(value, PurePath):
        value = str(value)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    escapes = {"\r": "\\r", "\n": "\\n", "\t": "\\t"}
    return "".join(
        escapes.get(char, f"\\u{ord(char):04x}")
        if ord(char) < 32 or 127 <= ord(char) < 160 or char in "\u2028\u2029"
        else char
        for char in value
    )


def lines(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict) and value:
        return [
            line
            for key, item in value.items()
            for line in lines(item, f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(value, list) and value:
        return [
            line for index, item in enumerate(value) for line in lines(item, f"{prefix}[{index}]")
        ]
    return [f"{text_value(prefix)}={text_value(value)}"]


def public_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    # Cleanup already returns all records. Add counts and usable paths in the CLI copy.
    if "candidates" in result and "state_dir" in result:
        for key in ("skipped", "failures"):
            records = []
            for item in result[key]:
                record = dict(item)
                record["job_directory"] = str(
                    PurePath(result["state_dir"]) / "jobs" / item["job_id"]
                )
                records.append(record)
            result[key] = records
            result[f"{key}_count"] = len(records)
    if result.get("status") == "review_required":
        result["review_hint"] = (
            "Review review.md and edit phrases.txt before running transcribe --phrase-file."
        )
    return result


def emit(payload: dict[str, Any], *, json_output: bool) -> None:
    result = public_result(payload)
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("\n".join(lines(result)))

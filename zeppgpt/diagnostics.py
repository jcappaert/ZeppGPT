"""Secret redaction, payload persistence, and schema inventory helpers."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SECRET_KEY = re.compile(
    r"(?:^|_)(?:app_?token|access_?token|refresh_?token|authorization|cookie|password|secret)(?:$|_)",
    re.IGNORECASE,
)


def redact(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    secret_values = tuple(secret for secret in secrets if secret)
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if SECRET_KEY.search(str(key))
                else redact(item, secrets=secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets=secret_values) for item in value]
    if isinstance(value, tuple):
        return [redact(item, secrets=secret_values) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            result = result.replace(secret, "<redacted>")
        return result
    return value


def field_inventory(payload: Any) -> list[dict[str, Any]]:
    """List payload paths and value types without exposing the values."""
    entries: dict[str, Counter[str]] = {}
    max_lengths: dict[str, int] = {}

    def visit(value: Any, path: str) -> None:
        value_type = _type_name(value)
        entries.setdefault(path or "$", Counter())[value_type] += 1
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}" if path else str(key)
                visit(item, child)
        elif isinstance(value, list):
            max_lengths[path or "$"] = max(max_lengths.get(path or "$", 0), len(value))
            for item in value:
                visit(item, f"{path}[]" if path else "[]")
        elif isinstance(value, str):
            max_lengths[path or "$"] = max(max_lengths.get(path or "$", 0), len(value))

    visit(payload, "")
    result: list[dict[str, Any]] = []
    for path in sorted(entries):
        entry: dict[str, Any] = {
            "path": path,
            "types": dict(sorted(entries[path].items())),
        }
        if path in max_lengths:
            entry["max_length"] = max_lengths[path]
        result.append(entry)
    return result


def create_diagnostic_directory(root: str | Path = "diagnostics") -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root_path = Path(root)
    candidate = root_path / timestamp
    suffix = 1
    while candidate.exists():
        candidate = root_path / f"{timestamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def write_json(
    path: str | Path,
    payload: Any,
    *,
    secrets: Iterable[str] = (),
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sanitized = redact(payload, secrets=secrets)
    destination.write_text(
        json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__

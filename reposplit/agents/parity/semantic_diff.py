"""Semantic JSON diff with normalization masks for non-deterministic fields."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_MASKS: list[str] = [
    "token",
    "session_token",
    "access_token",
    "refresh_token",
    "created_at",
    "updated_at",
    "timestamp",
    "traceparent",
    "request_id",
    "trace_id",
]
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def normalize(value: Any, masks: list[str] | None = None) -> Any:
    masks = masks if masks is not None else DEFAULT_MASKS
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in masks:
                out[k] = f"<NORMALIZED_{k.upper()}>"
            else:
                out[k] = normalize(v, masks)
        return out
    if isinstance(value, list):
        return [normalize(v, masks) for v in value]
    if isinstance(value, str):
        if _ISO_DT.match(value):
            return "<NORMALIZED_DATETIME>"
        if _UUID.match(value):
            return "<NORMALIZED_UUID>"
    if isinstance(value, float):
        return round(value, 6)
    return value


def diff(a: Any, b: Any, path: str = "$") -> list[str]:
    """Human-readable list of differences between two normalized JSON values (a = expected/monolith)."""
    if type(a) is not type(b) and not (isinstance(a, int | float) and isinstance(b, int | float)):
        return [f"{path}: type {type(a).__name__} != {type(b).__name__} ({a!r} vs {b!r})"]
    if isinstance(a, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}.{k}: missing in service response")
            elif k not in a:
                out.append(f"{path}.{k}: unexpected key in service response")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    if a != b:
        return [f"{path}: {a!r} != {b!r}"]
    return []

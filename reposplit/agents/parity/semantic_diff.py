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
    "jwt",
    "api_key",
    "date_created",
    "last_modified",
    "etag",
    "last_login",
]
_ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ERROR_KEYS = {"detail", "error", "message"}


def _canonical_key(k: str) -> str:
    return k.lower().replace("_", "").replace("-", "")


def normalize(value: Any, masks: list[str] | None = None) -> Any:
    masks = masks if masks is not None else DEFAULT_MASKS
    canonical_masks = {_canonical_key(m): m for m in masks}

    if isinstance(value, dict):
        if len(value) == 1:
            (single_k, single_v), = value.items()
            if single_k.lower() in _ERROR_KEYS and isinstance(single_v, str):
                return {"<ERROR_DETAIL>": normalize(single_v, masks)}

        out: dict[str, Any] = {}
        for k, v in value.items():
            canon = _canonical_key(k)
            if canon in canonical_masks:
                matched_mask = canonical_masks[canon]
                out[k] = f"<NORMALIZED_{matched_mask.upper()}>"
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
        if a and b and all(isinstance(x, dict) for x in a) and all(isinstance(y, dict) for y in b):
            common_id_keys = [k for k in ("id", "sku", "code", "name", "key") if all(k in x for x in a) and all(k in y for y in b)]
            if common_id_keys:
                sort_key = common_id_keys[0]
                a_sorted = sorted(a, key=lambda x: str(x.get(sort_key, "")))
                b_sorted = sorted(b, key=lambda x: str(x.get(sort_key, "")))
                out = []
                for i, (x, y) in enumerate(zip(a_sorted, b_sorted, strict=True)):
                    out += diff(x, y, f"{path}[{i}]")
                return out
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    if a != b:
        return [f"{path}: {a!r} != {b!r}"]
    return []

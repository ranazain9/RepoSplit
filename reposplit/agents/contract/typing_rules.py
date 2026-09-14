"""Type inference for untyped legacy signatures and JSON body keys (shared with the Parity agent)."""

from __future__ import annotations

import re
from typing import Any

_INT_HINTS = {"int", "Integer"}
_FLOAT_HINTS = {"float", "Float", "Decimal"}
_BOOL_HINTS = {"bool", "Boolean"}
_LIST_HINTS = ("list", "List", "Sequence")

_NAME_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|_)(id|count|quantity|qty|stock|limit|offset|page)$"), "integer"),
    (re.compile(r"(^|_)(price|amount|total|subtotal|tax|rate|weight)$"), "number"),
    (re.compile(r"^(is|has)_"), "boolean"),
    (re.compile(r"(^|_)(items|lines|ids|tags|products)$"), "array"),
]

_SAMPLE_BY_NAME: dict[str, Any] = {
    "email": "alice@example.com",
    "password": "correct-horse-battery",
    "full_name": "Alice Example",
    "name": "Sample Item",
    "tenant_id": "acme",
    "status": "pending",
    "sku": "SKU-001",
    "currency": "USD",
    "items": [{"product_id": 1, "quantity": 2}],
}


def json_type(name: str, hint: str | None = None) -> str:
    if hint:
        h = hint.replace("Optional[", "").rstrip("]").split("|")[0].strip()
        if h in _INT_HINTS:
            return "integer"
        if h in _FLOAT_HINTS:
            return "number"
        if h in _BOOL_HINTS:
            return "boolean"
        if h.startswith(_LIST_HINTS):
            return "array"
        if h in ("dict", "Dict", "Mapping"):
            return "object"
        if h == "str":
            return "string"
    for pattern, jtype in _NAME_RULES:
        if pattern.search(name):
            return jtype
    return "string"


def proto_type(name: str, hint: str | None = None) -> str:
    return {
        "integer": "int64",
        "number": "double",
        "boolean": "bool",
        "array": "repeated string",
        "object": "string",
        "string": "string",
    }[json_type(name, hint)]


def sample_value(name: str, hint: str | None = None, seed: int = 1) -> Any:
    if name in _SAMPLE_BY_NAME:
        return _SAMPLE_BY_NAME[name]
    jtype = json_type(name, hint)
    if jtype == "integer":
        return seed
    if jtype == "number":
        return round(9.99 * seed, 2)
    if jtype == "boolean":
        return True
    if jtype == "array":
        return []
    if jtype == "object":
        return {}
    return f"sample-{name}"

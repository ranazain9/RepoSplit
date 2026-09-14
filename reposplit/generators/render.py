"""Jinja2 rendering helper for every generated artifact."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from reposplit.utils.naming import service_short, to_pascal, to_snake

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,
)
_env.filters["pascal"] = to_pascal
_env.filters["snake"] = to_snake
_env.filters["short"] = service_short


def render(template: str, **ctx) -> str:
    return _env.get_template(template).render(**ctx)

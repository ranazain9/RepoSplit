"""Naming helpers shared by several agents (snake/pascal case, service ports, domain lexicon)."""

from __future__ import annotations

import re

# Ordered so more specific domains win ties. Each entry: domain -> keywords found in file stems,
# class names and function names. "core" marks infrastructure that becomes the shared kernel.
DOMAIN_LEXICON: dict[str, list[str]] = {
    "payment": ["payment", "charge", "refund", "invoice", "billing", "stripe", "checkout_pay"],
    "order": ["order", "checkout", "cart", "basket", "purchase", "fulfil", "line_item"],
    "catalog": ["product", "catalog", "inventory", "stock", "sku", "category", "listing"],
    "user": ["user", "auth", "login", "account", "session", "profile", "register", "password", "identity"],
    "notification": ["notify", "notification", "email", "sms", "mailer", "webhook"],
    "shipping": ["ship", "delivery", "address", "carrier", "fulfillment"],
    "pricing": ["pricing", "tax", "discount", "coupon", "promo"],
    "core": ["app", "main", "wsgi", "config", "settings", "db", "database", "extensions", "util", "common", "seed"],
}

SERVICE_PORT_BASE = 8001


def to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).replace("-", "_").lower()


def to_pascal(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-\s]+", name) if part)


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", to_snake(text)) if t]


def score_domains(words: list[str]) -> dict[str, int]:
    scores: dict[str, int] = {d: 0 for d in DOMAIN_LEXICON}
    for w in words:
        for domain, keys in DOMAIN_LEXICON.items():
            for k in keys:
                if (
                    w == k
                    or w in (f"{k}s", f"{k}es", f"{k}ing", f"{k}ed")
                    or f"_{k}_" in f"_{w}_"
                    or w.startswith(f"{k}_")
                    or w.endswith(f"_{k}")
                ):
                    scores[domain] += 1
    return scores


def service_port(index: int) -> int:
    return SERVICE_PORT_BASE + index


def service_short(name: str) -> str:
    """'order_service' -> 'orders', 'user_service' -> 'users'."""
    base = name.removesuffix("_service")
    return base if base.endswith("s") else base + "s"

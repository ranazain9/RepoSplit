"""Pricing rules. Small on purpose: the sales-tax line is the classic 'missing logic' parity regression."""

TAX_RATE = 0.08


def compute_tax(subtotal):
    return round(subtotal * TAX_RATE, 2)

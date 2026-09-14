"""Payment domain - simulates a card provider. Declines any charge over 10,000."""

import hashlib

from db import db
from models import Payment

DECLINE_OVER = 10_000.0


def _call_provider(user_id, amount):
    """Stand-in for a real PSP call. Deterministic so parity tests are reproducible."""
    if amount > DECLINE_OVER:
        return None
    return hashlib.md5(f"{user_id}:{amount:.2f}".encode()).hexdigest()[:16]


def charge_payment(user_id, order_id, amount):
    ref = _call_provider(user_id, amount)
    if ref is None:
        return {"ok": False, "reason": "declined"}
    payment = Payment(user_id=user_id, order_id=order_id, amount=amount, status="captured", provider_ref=ref)
    db.session.add(payment)
    db.session.flush()
    return {"ok": True, "payment_id": payment.id, "provider_ref": ref}


def refund_payment(payment_id):
    """Compensating action for charge_payment."""
    payment = Payment.query.get(payment_id)
    if payment is None:
        return {"ok": False, "reason": "unknown payment"}
    payment.status = "refunded"
    db.session.flush()
    return {"ok": True, "payment_id": payment.id}


def get_payment(payment_id):
    payment = Payment.query.get(payment_id)
    return payment.to_dict() if payment else None

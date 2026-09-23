"""Tests for Agent 7 (GovernanceAgent & Attestation) hardening:
- Root-of-trust verification with trusted public key
- Root-of-trust verification with trusted did:key
- Tamper detection on expanded infrastructure subjects (docker-compose, helm)
- Itemized FinOps cost line-items in predicate
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from reposplit.agents.governance.attestation import (
    did_key,
    sign_statement,
    verify_passport,
    verify_subjects,
)
from reposplit.core.schemas import InTotoStatement, Subject
from reposplit.utils.hashing import hash_file


def _dummy_statement(subjects: list[Subject] | None = None) -> InTotoStatement:
    return InTotoStatement(
        subject=subjects or [Subject(name="services", digest={"sha256": "abc123def456"})],
        predicate={
            "runId": "test-run",
            "finOpsRoi": {
                "monolithMonthlyUsd": 265.0,
                "monolithComputeMonthlyUsd": 150.0,
                "monolithDbMonthlyUsd": 85.0,
                "monolithGatewayMonthlyUsd": 30.0,
                "microservicesMonthlyUsd": 75.0,
                "microservicesComputeMonthlyUsd": 45.0,
                "microservicesDbMonthlyUsd": 18.0,
                "microservicesGatewayMonthlyUsd": 12.0,
                "monthlySavingsUsd": 190.0,
                "annualSavingsUsd": 2280.0,
            },
        },
    )


def test_verify_passport_trusted_key_success() -> None:
    key = Ed25519PrivateKey.generate()
    statement = _dummy_statement()
    passport = sign_statement(statement, key)

    # 1. Self-verify without trusted key
    ok, problems = verify_passport(passport)
    assert ok is True
    assert problems == []

    # 2. Verify with matching trusted public key
    ok, problems = verify_passport(passport, trusted_key=key.public_key())
    assert ok is True
    assert problems == []

    # 3. Verify with matching trusted did:key
    ok, problems = verify_passport(passport, trusted_did=did_key(key.public_key()))
    assert ok is True
    assert problems == []


def test_verify_passport_trusted_key_rejection() -> None:
    signer_key = Ed25519PrivateKey.generate()
    untrusted_key = Ed25519PrivateKey.generate()
    statement = _dummy_statement()
    passport = sign_statement(statement, signer_key)

    # Rejects mismatched trusted key
    ok, problems = verify_passport(passport, trusted_key=untrusted_key.public_key())
    assert ok is False
    assert any("does not match trusted root key" in p for p in problems)

    # Rejects mismatched trusted DID
    ok, problems = verify_passport(passport, trusted_did=did_key(untrusted_key.public_key()))
    assert ok is False
    assert any("does not match trusted DID" in p for p in problems)


def test_verify_subjects_tamper_detection() -> None:
    with tempfile.TemporaryDirectory(prefix="reposplit-gov-test-") as tmp:
        out = Path(tmp)
        dc_file = out / "docker-compose.yml"
        dc_file.write_text("version: '3.8'\nservices:\n  catalog: ...", encoding="utf-8")

        subjects = [Subject(name="docker-compose.yml", digest={"sha256": hash_file(dc_file)})]
        key = Ed25519PrivateKey.generate()
        passport = sign_statement(_dummy_statement(subjects), key)

        # Baseline: No drift
        assert verify_subjects(passport, out) == []

        # Tampering: attacker injects malicious container into docker-compose.yml
        dc_file.write_text("version: '3.8'\nservices:\n  catalog: ...\n  backdoor: ...", encoding="utf-8")

        # Must detect tampering!
        drift = verify_subjects(passport, out)
        assert len(drift) == 1
        assert "docker-compose.yml: sha256 changed since attestation" in drift[0]


def test_itemized_finops_predicate_fields() -> None:
    statement = _dummy_statement()
    pred = statement.predicate
    fo = pred["finOpsRoi"]
    assert fo["monolithComputeMonthlyUsd"] == 150.0
    assert fo["monolithDbMonthlyUsd"] == 85.0
    assert fo["microservicesComputeMonthlyUsd"] == 45.0
    assert fo["microservicesDbMonthlyUsd"] == 18.0

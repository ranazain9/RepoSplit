"""in-toto v1 Statement + DSSE (Dead Simple Signing Envelope) with Ed25519.

    PAE(type, body) = "DSSEv1" SP len(type) SP type SP len(body) SP body
    signature       = Ed25519.sign(PAE(...))
    keyid           = sha256(raw public key)[:16]
    signer          = did:key:z<base58btc(0xed01 || raw public key)>
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from reposplit.core.schemas import DSSEEnvelope, DSSESignature, InTotoStatement, MigrationPassport
from reposplit.utils.hashing import canonical_json, sha256_hex

PAYLOAD_TYPE = "application/vnd.in-toto+json"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def did_key(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "did:key:z" + b58encode(b"\xed\x01" + raw)


def key_id(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sha256_hex(raw)[:16]


def load_or_create_key(path: Path | None, generated_dir: Path) -> tuple[Ed25519PrivateKey, Path]:
    if path and path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("signing key must be Ed25519")
        return key, path
    key = Ed25519PrivateKey.generate()
    generated_dir.mkdir(parents=True, exist_ok=True)
    target = path or generated_dir / "attestation_key.pem"
    target.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    (generated_dir / "attestation_key.pub").write_bytes(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return key, target


def pae(payload_type: str, payload: bytes) -> bytes:
    return b" ".join([b"DSSEv1", str(len(payload_type)).encode(), payload_type.encode(), str(len(payload)).encode(), payload])


def sign_statement(statement: InTotoStatement, key: Ed25519PrivateKey) -> MigrationPassport:
    payload = canonical_json(statement.model_dump(mode="json", by_alias=True))
    sig = key.sign(pae(PAYLOAD_TYPE, payload))
    public = key.public_key()
    envelope = DSSEEnvelope(
        payloadType=PAYLOAD_TYPE,
        payload=base64.b64encode(payload).decode("ascii"),
        signatures=[DSSESignature(keyid=key_id(public), sig=base64.b64encode(sig).decode("ascii"))],
    )
    pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return MigrationPassport(statement=statement, envelope=envelope, public_key_pem=pem, signer_did=did_key(public))


def verify_passport(
    passport: MigrationPassport,
    trusted_key: Ed25519PublicKey | str | Path | None = None,
    trusted_did: str | None = None,
) -> tuple[bool, list[str]]:
    """Verify the DSSE signature, statement consistency, and optional trusted root key / DID."""
    problems: list[str] = []
    public = serialization.load_pem_public_key(passport.public_key_pem.encode())
    if not isinstance(public, Ed25519PublicKey):
        return False, ["public key is not Ed25519"]

    if trusted_key is not None:
        if isinstance(trusted_key, (str, Path)):
            key_bytes = Path(trusted_key).read_bytes() if Path(str(trusted_key)).exists() else str(trusted_key).encode()
            trusted_pub = serialization.load_pem_public_key(key_bytes)
        else:
            trusted_pub = trusted_key
        if not isinstance(trusted_pub, Ed25519PublicKey):
            return False, ["trusted key must be Ed25519"]
        if public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) != trusted_pub.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ):
            problems.append("signer public key does not match trusted root key")

    if trusted_did and passport.signer_did != trusted_did:
        problems.append(f"signer DID {passport.signer_did} does not match trusted DID {trusted_did}")

    payload = base64.b64decode(passport.envelope.payload)
    expected = canonical_json(passport.statement.model_dump(mode="json", by_alias=True))
    if payload != expected:
        problems.append("envelope payload does not match the embedded statement")
    if did_key(public) != passport.signer_did:
        problems.append("signer DID does not match the public key")
    for signature in passport.envelope.signatures:
        if signature.keyid != key_id(public):
            problems.append(f"unknown keyid {signature.keyid}")
            continue
        try:
            public.verify(base64.b64decode(signature.sig), pae(passport.envelope.payloadType, payload))
        except Exception:  # noqa: BLE001 - cryptography raises InvalidSignature
            problems.append("invalid DSSE signature")
    if not passport.envelope.signatures:
        problems.append("no signatures")
    return not problems, problems


def verify_subjects(passport: MigrationPassport, output_dir: Path) -> list[str]:
    """Re-hash the artifacts named in the statement subjects and report any drift."""
    drift: list[str] = []
    for subject in passport.statement.subject:
        path = output_dir / subject.name
        if not path.exists():
            drift.append(f"{subject.name}: missing")
            continue
        digest = sha256_hex(path.read_bytes()) if path.is_file() else _hash_dir(path)
        if digest != subject.digest.get("sha256"):
            drift.append(f"{subject.name}: sha256 changed since attestation")
    return drift


def _hash_dir(path: Path) -> str:
    from reposplit.utils.hashing import hash_tree

    return hash_tree(path)


def load_passport(path: Path) -> MigrationPassport:
    return MigrationPassport.model_validate(json.loads(path.read_text(encoding="utf-8")))

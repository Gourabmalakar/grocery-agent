"""
Signing primitive for every mandate — Intent, IntentAmendment, Cart, Payment.

AP2-shaped, not AP2-networked: this demonstrates the protocol's core idea
(a mandate is a signed, hashable, tamper-evident claim) using a local ES256
keypair rather than joining the real W3C Verifiable Credential network. That
is a deliberate scope line — see the plan's "what I'd flag" section.

The keypair is generated on first use and kept under DATA/keys, outside git.
Losing it invalidates every previously-issued mandate's verifiability, which
is correct: a mandate signed by a key nobody can produce again should not be
trusted just because the JSON still parses.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.exceptions import InvalidSignature

from grocery import config

_PRIVATE_KEY_PATH = config.KEYS_DIR / "mandate_signing_key.pem"
_PUBLIC_KEY_PATH = config.KEYS_DIR / "mandate_signing_key.pub.pem"


def _load_or_create_keypair() -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    if _PRIVATE_KEY_PATH.exists():
        priv = serialization.load_pem_private_key(_PRIVATE_KEY_PATH.read_bytes(), password=None)
        assert isinstance(priv, ec.EllipticCurvePrivateKey)
        return priv, priv.public_key()

    priv = ec.generate_private_key(ec.SECP256R1())  # P-256, i.e. the "ES256" in ES256
    pub = priv.public_key()

    _PRIVATE_KEY_PATH.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    _PRIVATE_KEY_PATH.chmod(0o600)
    _PUBLIC_KEY_PATH.write_bytes(pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return priv, pub


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic serialisation so the same logical document always hashes
    and signs identically, regardless of dict insertion order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sign(payload: dict[str, Any]) -> dict[str, Any]:
    """Return payload with a `signature` block attached. The signature covers
    the payload exactly as given — sign after every field is final."""
    priv, _ = _load_or_create_keypair()
    digest = canonical_bytes(payload)
    der_sig = priv.sign(digest, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return {
        **payload,
        "signature": {
            "alg": "ES256",
            "sig": raw_sig.hex(),
            "content_hash": content_hash(payload),
        },
    }


def verify(signed_payload: dict[str, Any]) -> bool:
    """True iff the signature block matches the rest of the document under
    the current signing key. Never raises on a bad signature — a forged or
    corrupted mandate is a False, not an exception, so callers can't
    accidentally treat an unhandled exception as "probably fine"."""
    body = {k: v for k, v in signed_payload.items() if k != "signature"}
    sig_block = signed_payload.get("signature")
    if not isinstance(sig_block, dict):
        return False
    try:
        raw_sig = bytes.fromhex(sig_block["sig"])
        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = asym_utils.encode_dss_signature(r, s)
        _, pub = _load_or_create_keypair()
        pub.verify(der_sig, canonical_bytes(body), ec.ECDSA(hashes.SHA256()))
        return sig_block.get("content_hash") == content_hash(body)
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def save_issued(kind: str, mandate_id: str, signed_payload: dict[str, Any]) -> Path:
    """Mandates are append-only records once issued — see config.MANDATES_ISSUED.
    Never overwritten; a revision issues a new mandate/amendment instead."""
    path = config.MANDATES_ISSUED / f"{kind}-{mandate_id}.json"
    if path.exists():
        raise FileExistsError(f"refusing to overwrite an already-issued mandate: {path}")
    path.write_text(json.dumps(signed_payload, indent=2, sort_keys=True))
    return path

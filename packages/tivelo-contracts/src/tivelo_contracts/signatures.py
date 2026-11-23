"""Public verification of detached Tivelo envelopes. Trust keys are supplied by the caller."""

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .base import parse_json

Purpose = Literal["engine-release", "desktop-core-release", "license"]
_DOMAINS = {
    "engine-release": b"tivelo-engine-release-v1\0",
    "desktop-core-release": b"tivelo-desktop-core-release-v1\0",
    "license": b"tivelo-license-v1\0",
}
MAX_ENVELOPE = 64 * 1024


def decode_base64url(value: str, *, size: int | None = None) -> bytes:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_ENVELOPE
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise ValueError("Invalid base64url field")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid base64url encoding") from None
    if (size is not None and len(decoded) != size) or base64.urlsafe_b64encode(decoded).rstrip(
        b"="
    ).decode("ascii") != value:
        raise ValueError("Invalid base64url length or padding bits")
    return decoded


@dataclass(frozen=True)
class VerifiedClaims:
    kid: str
    claims: dict


def verify_envelope(
    envelope: bytes, *, purpose: Purpose, public_keys: Mapping[str, bytes]
) -> VerifiedClaims:
    """Authenticate the original JCS bytes for one fixed purpose. Claims need domain validation."""
    if purpose not in _DOMAINS or len(envelope) > MAX_ENVELOPE:
        raise ValueError("Unsupported signature purpose or oversized envelope")
    try:
        outer = parse_json(envelope.decode("utf-8"))
        if not isinstance(outer, dict) or set(outer) != {"signed", "signature"}:
            raise ValueError
        signed = decode_base64url(outer["signed"])
        signature = decode_base64url(outer["signature"], size=64)
        payload = parse_json(signed.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"protected", "claims"}
            or not isinstance(payload["claims"], dict)
        ):
            raise ValueError
        header = payload["protected"]
        if not isinstance(header, dict) or set(header) != {"version", "alg", "type", "kid"}:
            raise ValueError
        if (
            type(header["version"]) is not int
            or header["version"] != 1
            or header["alg"] != "Ed25519"
            or header["type"] != purpose
        ):
            raise ValueError
        kid = header["kid"]
        if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", kid):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid signed envelope or protected header") from None
    if kid not in public_keys:
        raise ValueError("Unknown signing key for the requested purpose")
    try:
        import rfc8785
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise ValueError("Tivelo signature verification dependencies are unavailable") from None
    try:
        Ed25519PublicKey.from_public_bytes(public_keys[kid]).verify(
            signature, _DOMAINS[purpose] + signed
        )
        if signed != rfc8785.dumps(payload):
            raise ValueError
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm, RecursionError):
        raise ValueError("Signature or canonical signed bytes failed verification") from None
    return VerifiedClaims(kid=kid, claims=payload["claims"])

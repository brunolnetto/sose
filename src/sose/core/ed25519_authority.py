"""Ed25519 transition receipts: verifiers hold only the public key (#494, #545).

With ``TransitionAuthority`` (HMAC) the same secret signs and verifies, so anyone who
can check a receipt can also forge one. Here the Engine signs with an Ed25519
private key, and a downstream verifier (``verify``, ``verify_history``, the
organization snapshot) is built from the public key alone. A verifier cannot issue
an accepted receipt: it has no ``_signed`` capability, and a receipt signed by
any other key fails.

Ed25519 receipts carry the claim ``"algorithm": "ed25519"`` and a 128-hex-digit
signature. An HMAC authority refuses them and an Ed25519 verifier refuses HMAC
receipts ("signature missing"). Receipts issued before this module are unchanged.

Needs the optional ``cryptography`` package: ``pip install sose[attestation]``.
"""
from __future__ import annotations

from dataclasses import dataclass

from sose.core.receipt_claims import _encoded
from sose.core.transition_authority import ReceiptAuthority, require_ids

ALGORITHM = "ed25519"


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as error:
        raise ImportError(
            "Ed25519 transition receipts need the 'cryptography' package: "
            "pip install sose[attestation]"
        ) from error
    return InvalidSignature, Ed25519PrivateKey, Ed25519PublicKey


def _well_formed(signature, data: dict) -> bool:
    return (
        isinstance(signature, str) and len(signature) == 128
        and data.get("algorithm") == ALGORITHM
    )


@dataclass(frozen=True, slots=True, repr=False)
class Ed25519TransitionVerifier(ReceiptAuthority):
    """Verifies Ed25519 receipts from the issuer's raw 32-byte public key.

    It authenticates receipts exactly like the issuing authority does, and it
    refuses to issue any: an Engine or job configured with it fails closed at the
    first transition or attribute save (nothing is written).
    """

    issuer_id: str
    key_id: str
    public_key: bytes

    def __post_init__(self) -> None:
        require_ids(self)
        if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
            raise ValueError("an Ed25519 verifier needs a raw 32-byte public key")
        _crypto()[2].from_public_bytes(self.public_key)

    def _signed(self, claims: dict) -> dict:
        raise PermissionError("an Ed25519 verifier holds no private key and cannot issue receipts")

    def _well_formed(self, signature, data: dict) -> bool:
        return _well_formed(signature, data)

    def _valid(self, signature: str, data: dict) -> bool:
        invalid_signature, _private, public = _crypto()
        try:
            public.from_public_bytes(self.public_key).verify(
                bytes.fromhex(signature), _encoded(data),
            )
        except (invalid_signature, ValueError):
            return False
        return True


@dataclass(frozen=True, slots=True, repr=False)
class Ed25519TransitionAuthority(ReceiptAuthority):
    """Issues Ed25519 receipts from a raw 32-byte private key (seed).

    Hand ``verifier()`` (public key only) to whoever needs to check receipts.
    """

    issuer_id: str
    key_id: str
    private_key: bytes

    def __post_init__(self) -> None:
        require_ids(self)
        if not isinstance(self.private_key, bytes) or len(self.private_key) != 32:
            raise ValueError("an Ed25519 authority needs a raw 32-byte private key")
        _crypto()[1].from_private_bytes(self.private_key)

    def verifier(self) -> Ed25519TransitionVerifier:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        public = _crypto()[1].from_private_bytes(self.private_key).public_key()
        return Ed25519TransitionVerifier(
            self.issuer_id, self.key_id, public.public_bytes(Encoding.Raw, PublicFormat.Raw),
        )

    def _signed(self, claims: dict) -> dict:
        signed = {**claims, "algorithm": ALGORITHM}
        key = _crypto()[1].from_private_bytes(self.private_key)
        return {**signed, "signature": key.sign(_encoded(signed)).hex()}

    def _well_formed(self, signature, data: dict) -> bool:
        return _well_formed(signature, data)

    def _valid(self, signature: str, data: dict) -> bool:
        return self.verifier()._valid(signature, data)

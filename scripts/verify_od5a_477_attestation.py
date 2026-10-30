"""Detached external-issuer authenticity gate for #477 nonconfirmatory bundle.

A valid SHA-256 index proves consistency, not authorship. Independently pinned
public-key material and a detached private-key signature establish issuer proof.
Scientific admission remains NOT_ADMITTED, regardless of the signature.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from tempfile import TemporaryDirectory
from hashlib import sha256
from pathlib import Path

from scripts.verify_od5a_477_evidence import verify

SCHEMA = "sose-od5a-477-external-recovery-attestation-v1"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n").encode("utf-8")


def statement(root: Path, *, issuer_id: str) -> bytes:
    """Derive all bundle claims from independently reverified raw arm files."""
    if not isinstance(issuer_id, str) or not issuer_id.strip() or issuer_id != issuer_id.strip():
        raise ValueError("explicit external issuer_id required")
    index = verify(root)
    if (index.get("signature_status") != "UNSIGNED_NO_TRUSTED_ISSUER"
        or index.get("scientific_admission") != "NOT_ADMITTED"
        or index.get("evidence_class") != "NON_CONFIRMATORY"):
        raise ValueError("external attestation only supports nonconfirmatory unsigned evidence")
    return _canonical({
        "schema": SCHEMA,
        "issuer_id": issuer_id,
        "index_sha256": sha256(_canonical(index)).hexdigest(),
        "source_git_oid": index["source_git_oid"],
        "uv_lock_sha256": index["uv_lock_sha256"],
        "evidence_class": "NON_CONFIRMATORY",
        "scientific_admission": "NOT_ADMITTED",
    })


def _trusted_key_snapshot(public_key: Path) -> tuple[bytes, str]:
    """Canonical SPKI and fingerprint derived from ONE public-key byte snapshot.

    Never fingerprint a path and then let a verifier reopen that path:
    a symlink-target or file replacement between the two would bypass the pin.
    """
    try:
        original = public_key.read_bytes()
        result = subprocess.run(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=original, check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("trusted public key or OpenSSL cannot be loaded") from exc
    if not result.stdout:
        raise ValueError("trusted public key has no SubjectPublicKeyInfo")
    der = result.stdout
    return der, sha256(der).hexdigest()


def public_key_fingerprint(public_key: Path) -> str:
    """SHA-256 over DER SubjectPublicKeyInfo from a single immutable read."""
    return _trusted_key_snapshot(public_key)[1]


def verify_attestation(
    root: Path, *, issuer_id: str, trusted_public_key: Path,
    expected_key_sha256: str, signature_path: Path,
    expected_source_git_oid: str, expected_uv_lock_sha256: str,
) -> dict:
    """Fail closed; never import a signer key or fingerprint from evidence."""
    if not isinstance(expected_key_sha256, str) or not _HEX64.fullmatch(expected_key_sha256):
        raise ValueError("independently pinned 64-digit key SHA-256 is mandatory")
    if not isinstance(expected_source_git_oid, str) or re.fullmatch(r"[0-9a-f]{40}", expected_source_git_oid) is None:
        raise ValueError("independently pinned source Git OID required")
    if not isinstance(expected_uv_lock_sha256, str) or _HEX64.fullmatch(expected_uv_lock_sha256) is None:
        raise ValueError("independently pinned uv.lock SHA-256 required")
    trusted_der, actual_fingerprint = _trusted_key_snapshot(trusted_public_key)
    if actual_fingerprint != expected_key_sha256:
        raise ValueError("external trust root fingerprint mismatch")
    content = statement(root, issuer_id=issuer_id)
    claims = json.loads(content)
    # Both pins are provided by the auditor; do not infer them from the bundle
    # or from the signature. A valid signature can cover the wrong source.
    if claims["source_git_oid"] != expected_source_git_oid:
        raise ValueError("independently pinned source Git OID mismatch")
    if claims["uv_lock_sha256"] != expected_uv_lock_sha256:
        raise ValueError("independently pinned uv.lock SHA-256 mismatch")
    try:
        signature_bytes = signature_path.read_bytes()
    except OSError as exc:
        raise ValueError("external detached signature missing or empty") from exc
    if not signature_bytes:
        raise ValueError("external detached signature missing or empty")
    try:
        # Normalize the SAME pinned SPKI bytes; never re-read the user-supplied
        # public-key path during openssl dgst verification.
        normalized = subprocess.run(
            ["openssl", "pkey", "-pubin", "-inform", "DER", "-outform", "PEM"],
            input=trusted_der, check=True, capture_output=True,
        ).stdout
        if not normalized:
            raise ValueError("pinned trusted public key normalization failed")
        with TemporaryDirectory(prefix="sose-477-trusted-snapshot-") as temp:
            key_file = Path(temp) / "verified-public.pem"
            signature_file = Path(temp) / "signature.bin"
            key_file.write_bytes(normalized)
            signature_file.write_bytes(signature_bytes)
            checked = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", str(key_file),
                 "-signature", str(signature_file)],
                input=content, capture_output=True, check=False,
            )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("OpenSSL signature verifier unavailable") from exc
    if checked.returncode != 0 or b"Verified OK" not in checked.stdout:
        raise ValueError("external detached signature invalid for current bundle")
    return {
        "attestation_schema": SCHEMA,
        "signature_status": "VERIFIED_DETACHED_EXTERNAL_KEY",
        "issuer_id": issuer_id,
        "issuer_public_key_sha256": actual_fingerprint,
        "statement_sha256": sha256(content).hexdigest(),
        "auditor_provenance_pins_matched": True,
        "source_git_oid": expected_source_git_oid,
        "uv_lock_sha256": expected_uv_lock_sha256,
        "evidence_class": "NON_CONFIRMATORY",
        "scientific_admission": "NOT_ADMITTED",
        # This is AUTHENTICITY of one bounded bundle, not proof that all #477
        # operational/cost/fairness faults were tested or scientifically admitted.
        "issue_477_fully_certified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--issuer-id", required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--emit-statement", type=Path)
    actions.add_argument("--signature", type=Path)
    parser.add_argument("--trusted-public-key", type=Path)
    parser.add_argument("--expected-key-sha256")
    parser.add_argument("--expected-source-git-oid")
    parser.add_argument("--expected-uv-lock-sha256")
    args = parser.parse_args()
    if args.emit_statement is not None:
        args.emit_statement.write_bytes(statement(args.bundle, issuer_id=args.issuer_id))
        return 0
    if args.trusted_public_key is None or args.expected_key_sha256 is None:
        parser.error("verification requires external public key AND pinned fingerprint")
    if args.expected_source_git_oid is None or args.expected_uv_lock_sha256 is None:
        parser.error("verification requires source Git OID and uv.lock SHA-256 pins")
    result = verify_attestation(
        args.bundle, issuer_id=args.issuer_id,
        trusted_public_key=args.trusted_public_key,
        expected_key_sha256=args.expected_key_sha256,
        signature_path=args.signature,
        expected_source_git_oid=args.expected_source_git_oid,
        expected_uv_lock_sha256=args.expected_uv_lock_sha256,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

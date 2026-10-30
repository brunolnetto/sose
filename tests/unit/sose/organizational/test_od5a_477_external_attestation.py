"""#477: external detached issuer cannot be self-asserted by bundle contents."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from scripts.verify_od5a_477_attestation import (
    public_key_fingerprint, statement, verify_attestation,
)
from tests.unit.sose.organizational.test_od5a_477_recovery_evidence import _bundle


ISSUER = "od5a:operational-validation-issuer"


@pytest.fixture
def signed_bundle(tmp_path):
    if shutil.which("openssl") is None:
        pytest.fail("OpenSSL mandatory for detached issuer authenticity gate")
    root = _bundle(tmp_path / "raw-five-arm-evidence")
    private = tmp_path / "private-test-only.pem"
    public = tmp_path / "public-trusted.pem"
    signature = tmp_path / "detached-issuer.sig"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
         "-out", str(private)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private),
         "-out", str(signature)],
        input=statement(root, issuer_id=ISSUER), capture_output=True, check=True,
    )
    return root, public, signature, public_key_fingerprint(public)


def _verify(data, **options):
    root, public, signature, fingerprint = data
    return verify_attestation(
        root, issuer_id=options.get("issuer_id", ISSUER),
        trusted_public_key=options.get("public", public),
        signature_path=options.get("signature", signature),
        expected_key_sha256=options.get("fingerprint", fingerprint),
        expected_source_git_oid=options.get("source_oid", "f" * 40),
        expected_uv_lock_sha256=options.get("lock_sha256", "e" * 64),
    )


def test_detached_signature_requires_independently_pinned_trust_root(signed_bundle):
    result = _verify(signed_bundle)
    assert result["signature_status"] == "VERIFIED_DETACHED_EXTERNAL_KEY"
    assert result["issuer_id"] == ISSUER
    assert result["evidence_class"] == "NON_CONFIRMATORY"
    assert result["scientific_admission"] == "NOT_ADMITTED"
    assert result["issue_477_fully_certified"] is False
    assert result["auditor_provenance_pins_matched"] is True
    assert result["source_git_oid"] == "f" * 40
    assert result["uv_lock_sha256"] == "e" * 64


def test_mutated_valid_evidence_invalidates_detached_signature(signed_bundle):
    root = signed_bundle[0]
    baseline = root / "baseline.json"
    record = json.loads(baseline.read_text())
    # Still internally coherent: only the detached external signature detects
    # modification to the originally approved experiment identity.
    record["model_spec_hash"] = "9" * 64
    baseline.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="external detached signature invalid"):
        _verify(signed_bundle)


def test_bundle_cannot_replace_independent_pinned_key_fingerprint(signed_bundle):
    with pytest.raises(ValueError, match="trust root fingerprint mismatch"):
        _verify(signed_bundle, fingerprint="f" * 64)


def test_wrong_issuer_identity_and_empty_signature_fail_closed(signed_bundle, tmp_path):
    with pytest.raises(ValueError, match="external detached signature invalid"):
        _verify(signed_bundle, issuer_id="foreign:untrusted")
    empty = tmp_path / "absent.sig"
    with pytest.raises(ValueError, match="external detached signature missing"):
        _verify(signed_bundle, signature=empty)


def test_unpinned_external_key_cannot_be_used_as_root(signed_bundle):
    with pytest.raises(ValueError, match="independently pinned"):
        _verify(signed_bundle, fingerprint="")


def test_no_scientific_promotion_by_attestation(signed_bundle):
    root = signed_bundle[0]
    record = json.loads((root / "baseline.json").read_text())
    record["scientific_admission"] = "ADMITTED"
    (root / "baseline.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="provenance"):
        _verify(signed_bundle)


def test_replaced_public_key_after_fingerprint_cannot_bypass_external_pin(
    signed_bundle, tmp_path, monkeypatch,
):
    """P1: key path replacement between pinning and signature check fails shut.

    The attacker substitutes a different public key and its own valid detached
    signature while preserving the independently approved legitimate key hash.
    Reopening the same mutable pathname at verification would incorrectly pass.
    """
    from scripts import verify_od5a_477_attestation as attestation

    root, trusted_key, signature_path, trusted_fingerprint = signed_bundle
    forged_private = tmp_path / "untrusted-private.pem"
    forged_public = tmp_path / "untrusted-public.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA",
         "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(forged_private)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(forged_private),
         "-pubout", "-out", str(forged_public)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(forged_private),
         "-out", str(signature_path)],
        input=statement(root, issuer_id=ISSUER),
        capture_output=True, check=True,
    )

    original_statement = attestation.statement
    switched = {"done": False}

    def replace_key_mid_verification(*args, **kwargs):
        output = original_statement(*args, **kwargs)
        trusted_key.write_bytes(forged_public.read_bytes())
        switched["done"] = True
        return output

    monkeypatch.setattr(attestation, "statement", replace_key_mid_verification)
    with pytest.raises(ValueError, match="external detached signature invalid"):
        attestation.verify_attestation(
            root, issuer_id=ISSUER, trusted_public_key=trusted_key,
            expected_key_sha256=trusted_fingerprint,
            signature_path=signature_path,
            expected_source_git_oid="f" * 40,
            expected_uv_lock_sha256="e" * 64,
        )
    assert switched["done"]
    assert attestation.public_key_fingerprint(trusted_key) != trusted_fingerprint


@pytest.mark.parametrize("field,wrong,reason", [
    ("source_oid", "9" * 40, "independently pinned source Git OID mismatch"),
    ("lock_sha256", "8" * 64, "independently pinned uv.lock SHA-256 mismatch"),
])
def test_valid_signature_does_not_override_auditor_pins(
    signed_bundle, field, wrong, reason,
):
    with pytest.raises(ValueError, match=reason):
        _verify(signed_bundle, **{field: wrong})


@pytest.mark.parametrize("field,wrong,reason", [
    ("source_oid", "", "independently pinned source Git OID required"),
    ("source_oid", "z" * 40, "independently pinned source Git OID required"),
    ("lock_sha256", "", "independently pinned uv.lock SHA-256 required"),
    ("lock_sha256", "z" * 64, "independently pinned uv.lock SHA-256 required"),
])
def test_missing_or_malformed_auditor_pins_fail_closed(
    signed_bundle, field, wrong, reason,
):
    with pytest.raises(ValueError, match=reason):
        _verify(signed_bundle, **{field: wrong})


@pytest.mark.parametrize("mutated_field,new_value,reason", [
    ("git_source_oid", "1" * 40, "independently pinned source Git OID mismatch"),
    ("uv_lock_sha256", "2" * 64, "independently pinned uv.lock SHA-256 mismatch"),
])
def test_resigned_evidence_cannot_bypass_independent_source_pins(
    signed_bundle, mutated_field, new_value, reason,
):
    """A coherent five-arm bundle can carry a valid signature over wrong source."""
    from scripts.verify_od5a_477_evidence import ARMS

    root, _, signature, _ = signed_bundle
    for arm in ARMS:
        path = root / f"{arm}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record[mutated_field] = new_value
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign",
         str(root.parent / "private-test-only.pem"), "-out", str(signature)],
        input=statement(root, issuer_id=ISSUER),
        capture_output=True, check=True,
    )
    with pytest.raises(ValueError, match=reason):
        _verify(signed_bundle)

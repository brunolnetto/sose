"""#494/#545: Ed25519 receipts, checked by a verifier that holds only the public key.

The Engine signs transition and attribute receipts with a private key; verification
(``verify_history``, the organization snapshot) needs only the public key, and a
verifier cannot issue a receipt anyone accepts.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

pytest.importorskip("cryptography")

from sose.backends.simpy import SimPyBackend  # noqa: E402
from sose.composition.organization_jobs import build_attested_component_jobs  # noqa: E402
from sose.composition.organization_snapshot import snapshot_organization  # noqa: E402
from sose.core.attestation_chain import ATTRIBUTES_EVENT  # noqa: E402
from sose.core.ed25519_authority import (  # noqa: E402
    Ed25519TransitionAuthority,
    Ed25519TransitionVerifier,
)
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority  # noqa: E402
from sose.examples.catalog import builtin_catalog  # noqa: E402
from sose.jobs.runner import SimulationJob  # noqa: E402
from sose.persistence.memory import MemoryPersistence  # noqa: E402
from tests.integration.sose.composition.test_attested_component_jobs import opener  # noqa: E402,F401
from tests.support import organization_workers as org  # noqa: E402

# ``opener`` is an imported fixture requested by name.
# ruff: noqa: F811
NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)
SIGNER = Ed25519TransitionAuthority("engine:ed25519", "ed25519-key:v1", bytes(range(32)))
VERIFIER = SIGNER.verifier()
CHAIN = ("entity.state_transition", ATTESTATION_EVENT, ATTRIBUTES_EVENT)


def _run(store, authority, *, name="warehouse_fulfillment", triggers=12):
    job = SimulationJob(job_id=f"job-{name}", definition=builtin_catalog().get(name),
                        persistence=store, backend_factory=lambda o: SimPyBackend(origin=o),
                        transition_authority=authority)
    job.initialize({"random_seed": 3})
    for index in range(triggers):
        job.run_trigger(trigger_id=f"t{index}", triggered_at=NOW)
    return job


def _verify_all(store, verifier):
    events = store.events()
    keys = {(e.entity_type, e.entity_id) for e in events
            if e.name in ("entity.state_transition", ATTRIBUTES_EVENT)}
    for key in keys:
        own = [e for e in events if (e.entity_type, e.entity_id) == key and e.name in CHAIN]
        verifier.verify_history(events=own, entity=store.entity(*key))
    return keys


@pytest.fixture(scope="module")
def signed_store():
    store = MemoryPersistence()
    _run(store, SIGNER)
    return store


def test_the_public_key_alone_verifies_transition_and_attribute_receipts(signed_store):
    assert _verify_all(signed_store, VERIFIER)
    receipts = [e for e in signed_store.events() if e.name in (ATTESTATION_EVENT, ATTRIBUTES_EVENT)]
    assert {e.name for e in receipts} == {ATTESTATION_EVENT, ATTRIBUTES_EVENT}
    assert all(e.payload["algorithm"] == "ed25519" and len(e.payload["signature"]) == 128
               for e in receipts)
    rebuilt = Ed25519TransitionVerifier("engine:ed25519", "ed25519-key:v1", VERIFIER.public_key)
    assert _verify_all(signed_store, rebuilt)


def test_a_verifier_cannot_issue_receipts_and_writes_nothing():
    store = MemoryPersistence()
    job = SimulationJob(job_id="job-v", definition=builtin_catalog().get("warehouse_fulfillment"),
                        persistence=store, backend_factory=lambda o: SimPyBackend(origin=o),
                        transition_authority=VERIFIER)
    job.initialize({"random_seed": 3})
    before = (list(store.events()), sorted((e.entity_type, e.id, e.version) for e in store.entities()))
    with pytest.raises(PermissionError, match="cannot issue receipts"):
        for index in range(12):
            job.run_trigger(trigger_id=f"t{index}", triggered_at=NOW)
    assert not any(e.name in (ATTESTATION_EVENT, ATTRIBUTES_EVENT) for e in store.events())
    assert sorted((e.entity_type, e.id, e.version) for e in store.entities()) == before[1]


def test_receipts_signed_by_another_key_with_the_same_ids_fail(signed_store):
    impostor = Ed25519TransitionAuthority("engine:ed25519", "ed25519-key:v1", b"\x07" * 32)
    forged_store = MemoryPersistence()
    _run(forged_store, impostor)
    with pytest.raises(ValueError, match="signature invalid"):
        _verify_all(forged_store, VERIFIER)


def test_hmac_and_ed25519_receipts_are_not_interchangeable(signed_store):
    hmac = TransitionAuthority("engine:ed25519", "ed25519-key:v1", b"0123456789abcdef" * 2)
    with pytest.raises(ValueError, match="signature missing"):
        _verify_all(signed_store, hmac)
    hmac_store = MemoryPersistence()
    _run(hmac_store, hmac)
    with pytest.raises(ValueError, match="signature missing"):
        _verify_all(hmac_store, VERIFIER)


def test_a_tampered_ed25519_receipt_fails():
    store = MemoryPersistence()
    _run(store, SIGNER)
    for name, field in ((ATTESTATION_EVENT, "to_state"), (ATTRIBUTES_EVENT, "state")):
        receipt = next(e for e in store.events() if e.name == name)
        index = store._state.events.index(receipt)
        store._state.events[index] = replace(receipt, payload={**receipt.payload, field: "forged"})
        with pytest.raises(ValueError, match="signature invalid"):
            _verify_all(store, VERIFIER)
        store._state.events[index] = receipt
    stripped = next(e for e in store.events() if e.name == ATTESTATION_EVENT)
    index = store._state.events.index(stripped)
    payload = {k: v for k, v in stripped.payload.items() if k != "algorithm"}
    store._state.events[index] = replace(stripped, payload=payload)
    with pytest.raises(ValueError, match="signature missing"):
        _verify_all(store, VERIFIER)


def test_the_organization_snapshot_authenticates_with_the_public_key(opener):
    store = opener()
    jobs = build_attested_component_jobs(org.definition(), store, backend_factory=org._backend,
                                         worker_id="w", transition_authority=SIGNER,
                                         **org.admission())
    for job in jobs:
        job.runner.initialize({})
        for index in range(org.TRIGGERS):
            job.runner.run_trigger(trigger_id=org.trigger_id(job.scheduled.job_id, index),
                                   triggered_at=org.T0)
    snap = snapshot_organization(org.definition(), store, transition_authority=VERIFIER,
                                 **org.admission())
    machines = [m for c in snap.components for m in c.machines]
    assert machines and all(m.authenticated for m in machines)
    assert snap.authenticated_key_id == "ed25519-key:v1"


@pytest.mark.parametrize("field, value, message", [
    ("public_key", b"short", "32-byte public key"),
    ("issuer_id", " ", "nonblank"),
])
def test_invalid_keys_and_ids_are_refused(field, value, message):
    arguments = {"issuer_id": "i", "key_id": "k", "public_key": VERIFIER.public_key, field: value}
    with pytest.raises(ValueError, match=message):
        Ed25519TransitionVerifier(**arguments)
    with pytest.raises(ValueError, match="32-byte private key"):
        Ed25519TransitionAuthority("i", "k", b"short")


def test_a_missing_cryptography_package_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "cryptography.exceptions", None)
    with pytest.raises(ImportError, match=r"sose\[attestation\]"):
        Ed25519TransitionVerifier("i", "k", VERIFIER.public_key)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_a_verifier_cannot_attest_an_attribute_save(backend, tmp_path):
    from sose.core.attestation_chain import AttributeAttestingPersistence
    from sose.domain.entity import Entity
    from sose.domain.registry import DomainRegistry
    from sose.persistence.sqlite import SQLitePersistence

    store = MemoryPersistence() if backend == "memory" else SQLitePersistence(tmp_path / "s.db")
    with store.transaction() as uow:
        uow.save_entity(Entity(id="e", entity_type="t", state="s", created_at=NOW, updated_at=NOW))
    wrapped = AttributeAttestingPersistence(store, authority=VERIFIER, owner_id="owner",
                                            registry=DomainRegistry())
    entity = replace(store.entity("t", "e"), attributes={"n": 1})
    with pytest.raises(PermissionError, match="cannot issue receipts"):
        with wrapped.transaction() as uow:
            uow.save_entity(entity)
    stored = store.entity("t", "e")
    assert (stored.attributes, stored.attribute_revision) == ({}, 0)
    assert not any(e.name == ATTRIBUTES_EVENT for e in store.events())


def test_an_upper_cased_signature_is_not_the_same_receipt(signed_store):
    receipt = next(e for e in signed_store.events() if e.name == ATTRIBUTES_EVENT)
    data = {**receipt.payload, "signature": receipt.payload["signature"].upper()}
    with pytest.raises(ValueError, match="signature missing"):
        VERIFIER._require_signed(data, "attribute receipt")

"""#494/#545: attested attribute-only saves, chained with transition receipts.

Domains that save entity attributes outside transitions become attestable: under a
TransitionAuthority the job wraps its domain persistence, each attribute-only save
bumps ``Entity.attribute_revision`` and issues a signed attribute receipt chained to
the previous receipt. Without an authority nothing changes.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.organization_jobs import build_attested_component_jobs
from sose.composition.organization_snapshot import snapshot_organization
from sose.core.attestation_chain import ATTRIBUTES_EVENT, AttributeAttestingPersistence
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.entity import Entity
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.codec import dumps
from sose.persistence.memory import MemoryPersistence
from tests.integration.sose.composition.test_attested_component_jobs import opener  # noqa: F401
from tests.support import organization_workers as org

# ``opener`` is an imported fixture requested by name.
# ruff: noqa: F811
NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)
ROOT = TransitionAuthority("engine:attributes", "attributes-key:v1", b"0123456789abcdef" * 2)
CHAIN = ("entity.state_transition", ATTESTATION_EVENT, ATTRIBUTES_EVENT)
# The seven catalog domains that save attributes outside transitions (#684 sweep).
ATTRIBUTE_WRITERS = ("construction", "field_service", "hospitality", "insurance", "transit",
                     "warehouse_fulfillment", "warehouse_management")


def _run(name, store, *, authority=ROOT, triggers=12):
    job = SimulationJob(job_id=f"job-{name}", definition=builtin_catalog().get(name),
                        persistence=store, backend_factory=lambda o: SimPyBackend(origin=o),
                        transition_authority=authority)
    job.initialize({"random_seed": 3})
    for index in range(triggers):
        job.run_trigger(trigger_id=f"t{index}", triggered_at=NOW)
    return job


def _verify_all(store, authority=ROOT):
    events = store.events()
    keys = {(e.entity_type, e.entity_id) for e in events
            if e.name in ("entity.state_transition", ATTRIBUTES_EVENT)}
    for key in keys:
        own = [e for e in events if (e.entity_type, e.entity_id) == key and e.name in CHAIN]
        authority.verify_history(events=own, entity=store.entity(*key))
    return keys


@pytest.mark.parametrize("name", ATTRIBUTE_WRITERS)
def test_a_domain_that_saves_attributes_runs_attested_and_verifies(name):
    store = MemoryPersistence()
    _run(name, store)
    assert _verify_all(store)
    assert any(e.name == ATTRIBUTES_EVENT for e in store.events())


@pytest.mark.slow
def test_every_catalog_domain_is_attestable():
    for name in builtin_catalog().names():
        store = MemoryPersistence()
        _run(name, store, triggers=30)
        _verify_all(store)


def test_without_an_authority_nothing_changes():
    store = MemoryPersistence()
    job = _run("warehouse_fulfillment", store, authority=None)
    assert job.domain_persistence is store
    assert not any(e.name in (ATTESTATION_EVENT, ATTRIBUTES_EVENT) for e in store.events())
    assert all(e.attribute_revision == 0 for e in store.entities())
    assert "attribute_revision" not in dumps(store.entities()[0])


def test_the_organization_snapshot_authenticates_an_attribute_writing_run(opener):
    store = opener()
    jobs = build_attested_component_jobs(org.definition(), store, backend_factory=org._backend,
                                         worker_id="w", transition_authority=ROOT,
                                         **org.admission())
    for job in jobs:
        job.runner.initialize({})
        for index in range(org.TRIGGERS):
            job.runner.run_trigger(trigger_id=org.trigger_id(job.scheduled.job_id, index),
                                   triggered_at=org.T0)
    snap = snapshot_organization(org.definition(), store, transition_authority=ROOT,
                                 **org.admission())
    machines = [m for c in snap.components for m in c.machines]
    assert machines and all(m.authenticated for m in machines)
    assert any(e.name == ATTRIBUTES_EVENT for e in store.events())


def _attributed_machine(store):
    receipt = next(e for e in store.events() if e.name == ATTRIBUTES_EVENT)
    return receipt.entity_type, receipt.entity_id


def test_an_attribute_save_bypassing_the_wrapper_fails_verification():
    store = MemoryPersistence()
    _run("warehouse_fulfillment", store)
    key = _attributed_machine(store)
    with store.transaction() as uow:                       # raw persistence, no attestation
        entity = uow.get_entity(*key)
        uow.save_entity(replace(entity, attributes={**entity.attributes, "forged": True}))
    with pytest.raises(ValueError, match="contradicts the stored entity"):
        _verify_all(store)


def test_rolling_back_a_revision_is_detected():
    store = MemoryPersistence()
    _run("warehouse_fulfillment", store)
    receipts = [e for e in store.events() if e.name == ATTRIBUTES_EVENT
                and e.payload["from_revision"] >= 1]
    later = receipts[0]
    key = (later.entity_type, later.entity_id)
    current = store.entity(*key)
    assert current.version == later.payload["version"]
    # Restore the projection one revision back; the later receipt stays in the log.
    with store.transaction() as uow:
        uow.save_entity(replace(current, attribute_revision=later.payload["from_revision"]))
    with pytest.raises(ValueError, match="contradicts the stored entity"):
        _verify_all(store)
    wrapped = AttributeAttestingPersistence(store, authority=ROOT, owner_id=later.owner_id)
    with pytest.raises(ValueError, match="replayed"):
        with wrapped.transaction() as uow:
            entity = uow.get_entity(*key)
            uow.save_entity(replace(entity, attributes={**entity.attributes, "x": 1}))


def test_a_state_change_without_a_transition_is_refused():
    store = MemoryPersistence()
    _run("warehouse_fulfillment", store)
    key = _attributed_machine(store)
    wrapped = AttributeAttestingPersistence(store, authority=ROOT, owner_id="job-warehouse_fulfillment")
    with pytest.raises(ValueError, match="needs an attested StateChart transition"):
        with wrapped.transaction() as uow:
            entity = uow.get_entity(*key)
            uow.save_entity(replace(entity, state="cancelled"))


def test_a_tampered_or_foreign_attribute_receipt_fails():
    store = MemoryPersistence()
    _run("warehouse_fulfillment", store)
    receipt = next(e for e in store.events() if e.name == ATTRIBUTES_EVENT)
    index = store._state.events.index(receipt)
    store._state.events[index] = replace(
        receipt, payload={**receipt.payload, "state": "tampered"})
    with pytest.raises(ValueError, match="signature invalid"):
        _verify_all(store)
    store._state.events[index] = receipt
    other = TransitionAuthority("engine:other", "other-key:v1", b"x" * 32)
    with pytest.raises(ValueError, match="issuer mismatch"):
        _verify_all(store, authority=other)


def test_the_caller_object_keeps_its_revision_in_step():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_entity(Entity(id="e", entity_type="t", state="s", created_at=NOW, updated_at=NOW))
    wrapped = AttributeAttestingPersistence(store, authority=ROOT, owner_id="owner")
    entity = store.entity("t", "e")
    for value in (1, 2):
        entity.attributes = {"n": value}
        with wrapped.transaction() as uow:
            uow.save_entity(entity)
        assert entity.attribute_revision == value == store.entity("t", "e").attribute_revision
    with wrapped.transaction() as uow:                     # nothing changed: no new receipt
        uow.save_entity(store.entity("t", "e"))
    assert sum(e.name == ATTRIBUTES_EVENT for e in store.events()) == 2
    ROOT.verify_history(events=list(store.events()), entity=store.entity("t", "e"))

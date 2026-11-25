"""#494/#545: attested attribute-only saves, chained with transition receipts.

Domains that save entity attributes outside transitions become attestable: under a
TransitionAuthority the job wraps its domain persistence, each attribute-only save
bumps ``Entity.attribute_revision`` and issues a signed attribute receipt chained to
the previous receipt. Without an authority nothing changes.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from hmac import new as hmac_new

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.organization_jobs import build_attested_component_jobs
from sose.composition.organization_snapshot import snapshot_organization
from sose.core.attestation_chain import (
    ATTRIBUTES_EVENT,
    AttributeAttestingPersistence,
    attribute_receipt_id,
    seal_attributes,
)
from sose.core.events import DomainEvent
from sose.core.transition_authority import (
    ATTESTATION_EVENT,
    TransitionAuthority,
    _attributes_sha256,
    _encoded,
)
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry
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
    # "t" has no StateChart in this registry, so its state is not chart-controlled.
    wrapped = AttributeAttestingPersistence(store, authority=ROOT, owner_id="owner",
                                            registry=DomainRegistry())
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


def test_a_version_zero_chain_starts_only_from_the_initial_state():
    """Review of #685: an unsigned state write at version 0 must not get signed."""
    store = MemoryPersistence()
    job = _run("warehouse_fulfillment", store, triggers=1)
    registry = job.domain_persistence.registry
    entity = next(e for e in store.entities() if e.version == 0
                  and e.attribute_revision == 0 and registry.supports(e.entity_type))
    with store.transaction() as uow:                       # raw, unsigned state write
        uow.save_entity(replace(entity, state="forged"))
    with pytest.raises(ValueError, match="initial chart state"):
        with job.domain_persistence.transaction() as uow:
            stored = uow.get_entity(entity.entity_type, entity.id)
            uow.save_entity(replace(stored, attributes={**stored.attributes, "z": 1}))


def test_before_a_registry_is_bound_a_root_save_is_stored_unsigned():
    """During initialize/seed no registry is bound: nothing is signed, nothing refused."""
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_entity(Entity(id="e", entity_type="t", state="s", created_at=NOW, updated_at=NOW))
    wrapped = AttributeAttestingPersistence(store, authority=ROOT, owner_id="owner")
    entity = store.entity("t", "e")
    entity.attributes = {"n": 1}
    with wrapped.transaction() as uow:
        uow.save_entity(entity)
    stored = store.entity("t", "e")
    assert (stored.attributes, stored.attribute_revision) == ({"n": 1}, 0)
    assert not any(e.name == ATTRIBUTES_EVENT for e in store.events())


def test_an_unsigned_root_is_still_checked_once_a_registry_is_bound():
    store = MemoryPersistence()
    job = _run("warehouse_fulfillment", store, triggers=1)
    registry = job.domain_persistence.registry
    entity = next(e for e in store.entities() if e.version == 0
                  and e.attribute_revision == 0 and registry.supports(e.entity_type))
    job.domain_persistence.registry = None                 # as during initialize/seed
    with job.domain_persistence.transaction() as uow:
        uow.save_entity(replace(entity, attributes={**entity.attributes, "seeded": 1}))
    with store.transaction() as uow:                       # raw, unsigned state write
        uow.save_entity(replace(store.entity(*_key(entity)), state="forged"))
    job.domain_persistence.registry = registry
    with pytest.raises(ValueError, match="initial chart state"):
        with job.domain_persistence.transaction() as uow:
            stored = uow.get_entity(*_key(entity))
            uow.save_entity(replace(stored, attributes={**stored.attributes, "z": 1}))


def _key(entity):
    return entity.entity_type, entity.id


def _chartless_chain(store):
    """A chart-less entity with one attribute receipt A(0,1) by ``owner-a``."""
    with store.transaction() as uow:
        uow.save_entity(Entity(id="e", entity_type="t", state="s", created_at=NOW, updated_at=NOW))
    wrapped = AttributeAttestingPersistence(store, authority=ROOT, owner_id="owner-a",
                                            registry=DomainRegistry())
    entity = store.entity("t", "e")
    entity.attributes = {"n": 1}
    with wrapped.transaction() as uow:
        uow.save_entity(entity)
    return store.entity("t", "e"), next(e for e in store.events() if e.name == ATTRIBUTES_EVENT)


def test_verification_refuses_a_chain_that_changes_owner():
    store = MemoryPersistence()
    first, receipt = _chartless_chain(store)
    after = replace(first, attributes={"n": 2}, attribute_revision=2)
    # Signed by the right key, but for another owner: the wrapper would refuse to
    # issue it ("foreign owner"); verification must refuse it too.
    foreign = seal_attributes(ROOT, before=first, after=after, owner_id="owner-b",
                              predecessor=receipt)
    with pytest.raises(ValueError, match="changes owner"):
        ROOT.verify_history(events=[receipt, foreign], entity=after)


def test_verification_refuses_an_attribute_receipt_naming_the_wrong_predecessor():
    store = MemoryPersistence()
    first, receipt = _chartless_chain(store)
    after = replace(first, attributes={"n": 2}, attribute_revision=6)
    claims = {
        "kind": "attributes", "issuer_id": ROOT.issuer_id, "key_id": ROOT.key_id,
        "owner_id": "owner-a", "entity_type": "t", "entity_id": "e", "version": 0,
        "from_revision": 5, "to_revision": 6, "state": "s",
        "attributes_sha256": _attributes_sha256(after), "updated_at": NOW.isoformat(),
        "predecessor_receipt_id": receipt.event_id,      # but (0, 5) implies A(0,5)
    }
    signature = hmac_new(ROOT.secret, _encoded(claims), sha256).hexdigest()
    skipped = DomainEvent(
        event_id=attribute_receipt_id("t", "e", 0, 6), name=ATTRIBUTES_EVENT,
        entity_type="t", entity_id="e", occurred_at=NOW, owner_id="owner-a",
        causation_id=receipt.event_id, payload={**claims, "signature": signature},
    )
    with pytest.raises(ValueError, match="chain is broken"):
        ROOT.verify_history(events=[receipt, skipped], entity=after)


def test_the_engine_path_refuses_a_tampered_entity_after_version_zero_attribute_receipts():
    """An entity at version 0, revision > 0 rewritten raw is refused by the Engine's
    prestate check (validate_prestate with the chart), not only by verify_history."""
    store = MemoryPersistence()
    job = _run("warehouse_fulfillment", store, triggers=1)
    registry = job.domain_persistence.registry
    root = next(e for e in store.entities() if e.version == 0
                and e.attribute_revision == 0 and registry.supports(e.entity_type))
    with job.domain_persistence.transaction() as uow:      # signs A(0,1) from the root
        uow.save_entity(replace(root, attributes={**root.attributes, "n": 1}))
    entity = store.entity(*_key(root))
    assert (entity.version, entity.attribute_revision) == (0, 1)
    owner = job.job_id
    for tampered in (replace(entity, state="forged"),
                     replace(entity, attributes={**entity.attributes, "forged": True})):
        with store.transaction() as uow:
            uow.save_entity(tampered)
            with pytest.raises(ValueError, match="contradicts the stored entity"):
                ROOT.validate_prestate(uow=uow, before=uow.get_entity(*_key(entity)),
                                       chart=registry.chart_for(deepcopy(tampered)),
                                       owner_id=owner)

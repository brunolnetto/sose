"""#494/#545: the organization snapshot verifies Engine transition receipts.

A real FulfillmentOrderChart is driven by an attested Engine (allocate -> start_pick ->
pack -> ship), so every transition carries an HMAC receipt chained to the previous
one. The snapshot, given the verifier authority, authenticates each attributed
machine and fails closed on a forged, unsigned, tampered or rewritten step.

The catalog's warehouse domains are NOT attestable end to end yet: their domain code
saves entity attributes outside transitions, which the authority refuses. These tests
drive the order chart directly; the job plumbing is covered with ``tutorial_job``.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.organization_snapshot import snapshot_organization
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.catalog import builtin_catalog
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence
from tests.integration.sose.composition.test_attested_component_jobs import opener  # noqa: F401
from tests.support import organization_workers as org

# ``opener`` is an imported fixture requested by name.
# ruff: noqa: F811
NOW = datetime(2026, 11, 22, 8, tzinfo=timezone.utc)
SECRET = b"0123456789abcdef0123456789abcdef"
ROOT = TransitionAuthority("engine:fulfillment", "fulfillment-key:v1", SECRET)
ORDER = "warehouse_fulfillment_order"
STEPS = ("allocate", "start_pick", "pack", "ship")


def _engine(store, authority):
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=5), scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType(ORDER, FulfillmentOrderChart))
    return Engine(context=context, registry=registry, persistence=store,
                  owner_id=org.FULFILL, transition_authority=authority)


def _shipped_order(store, authority=ROOT, order_id="order-1"):
    with store.transaction(owner_id=org.FULFILL) as uow:
        uow.save_entity(Entity(id=order_id, entity_type=ORDER, state="requested",
                               created_at=NOW, updated_at=NOW))
    engine = _engine(store, authority)
    for step in STEPS:
        engine.dispatch(Command(
            command_id=f"cmd:{order_id}:{step}", name=step, entity_type=ORDER,
            entity_id=order_id, owner_id=org.FULFILL, due_at=NOW, correlation_id="flow:1",
        ))
    return order_id


def snapshot(store, authority=ROOT):
    return snapshot_organization(org.definition(), store, transition_authority=authority,
                                 **org.admission())


def test_attested_machines_are_authenticated(opener):
    store = opener()
    _shipped_order(store)
    snap = snapshot(store)
    [machine] = snap.components[0].machines
    assert (machine.entity_id, machine.state, machine.authenticated) == ("order-1", "shipped", True)
    assert snap.authenticated_key_id == "fulfillment-key:v1"
    plain = snapshot(store, authority=None)
    assert plain.components[0].machines[0].authenticated is False
    assert plain.authenticated_key_id is None and plain.digest != snap.digest


def test_a_forged_transition_through_the_public_uow_fails_closed(opener):
    """The #545 falsifier: projection + matching deterministic event, no receipt."""
    store = opener()
    with store.transaction(owner_id=org.FULFILL) as uow:
        uow.save_entity(Entity(id="order-forged", entity_type=ORDER, state="shipped",
                               created_at=NOW, updated_at=NOW, version=1))
        uow.append_event(DomainEvent(
            event_id=deterministic_id("event", org.FULFILL, 5, ORDER, "order-forged", 1,
                                      "ship", "requested", "shipped"),
            name="entity.state_transition", entity_type=ORDER, entity_id="order-forged",
            occurred_at=NOW, tick=5, owner_id=org.FULFILL, correlation_id="forged",
            payload={"trigger": "ship", "from_state": "requested", "to_state": "shipped"},
        ))
    assert snapshot(store, authority=None).components[0].machines[0].state == "shipped"
    with pytest.raises(ValueError, match="receipt missing"):
        snapshot(store)


def test_a_verifier_with_another_key_rejects_the_receipts(opener):
    store = opener()
    _shipped_order(store)
    other = TransitionAuthority("engine:fulfillment", "fulfillment-key:v1", b"x" * 32)
    with pytest.raises(ValueError, match="signature invalid"):
        snapshot(store, authority=other)
    foreign = TransitionAuthority("engine:other", "other-key:v1", SECRET)
    with pytest.raises(ValueError, match="issuer mismatch"):
        snapshot(store, authority=foreign)


def test_attributes_rewritten_after_the_last_receipt_fail_closed(opener):
    store = opener()
    order_id = _shipped_order(store)
    with store.transaction(owner_id=org.FULFILL) as uow:
        entity = uow.get_entity(ORDER, order_id)
        uow.save_entity(replace(entity, attributes={**entity.attributes, "carrier": "forged"}))
    with pytest.raises(ValueError, match="contradicts the stored entity"):
        snapshot(store)


def test_a_tampered_receipt_fails_closed():
    store = MemoryPersistence()
    _shipped_order(store)
    receipts = [e for e in store.events() if e.name == ATTESTATION_EVENT]
    tampered = replace(receipts[1], payload={**receipts[1].payload, "to_state": "packed"})
    store._state.events[store._state.events.index(receipts[1])] = tampered
    with pytest.raises(ValueError, match="signature invalid"):
        snapshot(store)


def test_a_job_with_an_authority_issues_receipts_through_its_engine():
    store = MemoryPersistence()
    job = SimulationJob(
        job_id="tutorial", definition=builtin_catalog().get("tutorial_job"), persistence=store,
        backend_factory=lambda origin: SimPyBackend(origin=origin), transition_authority=ROOT,
    )
    job.initialize({"random_seed": 3})
    for index in range(3):
        job.run_trigger(trigger_id=f"t{index}", triggered_at=NOW)
    events = store.events()
    transitions = [e for e in events if e.name == "entity.state_transition"]
    receipts = {e.causation_id: e for e in events if e.name == ATTESTATION_EVENT}
    assert transitions and set(receipts) == {e.event_id for e in transitions}
    for event in transitions:
        entity = store.entity(event.entity_type, event.entity_id)
        own = [e for e in transitions if (e.entity_type, e.entity_id) == (
            event.entity_type, event.entity_id)]
        ROOT.verify_history(transitions=own, receipts_by_source=receipts, entity=entity)


def test_an_authority_with_a_domain_warehouse_is_refused():
    with pytest.raises(ValueError, match="one store"):
        SimulationJob(
            job_id="j", definition=builtin_catalog().get("tutorial_job"),
            persistence=MemoryPersistence(), backend_factory=lambda origin: None,
            domain_warehouse=object(), transition_authority=ROOT,
        )


def test_verify_history_refuses_an_empty_or_reordered_history():
    store = MemoryPersistence()
    _shipped_order(store)
    events = store.events()
    transitions = [e for e in events if e.name == "entity.state_transition"]
    receipts = {e.causation_id: e for e in events if e.name == ATTESTATION_EVENT}
    entity = store.entity(ORDER, "order-1")
    with pytest.raises(ValueError, match="no StateChart transition"):
        ROOT.verify_history(transitions=[], receipts_by_source=receipts, entity=entity)
    reordered = [transitions[1], transitions[0], *transitions[2:]]
    with pytest.raises(ValueError, match="chain is broken"):
        ROOT.verify_history(transitions=reordered, receipts_by_source=receipts, entity=entity)


def test_two_receipts_for_one_transition_fail_closed():
    store = MemoryPersistence()
    _shipped_order(store)
    [first, *_] = [e for e in store.events() if e.name == ATTESTATION_EVENT]
    with store.transaction(owner_id=org.FULFILL) as uow:
        uow.append_event(replace(first, event_id=deterministic_id("copy", first.event_id)))
    with pytest.raises(ValueError, match="two receipts"):
        snapshot(store)


def test_a_stray_receipt_elsewhere_does_not_deny_the_snapshot():
    """Review of #684 (N2): only receipts of attributed machines' transitions count."""
    store = MemoryPersistence()
    _shipped_order(store)
    with store.transaction(owner_id="owner:unrelated") as uow:
        for index in range(2):
            uow.append_event(DomainEvent(
                event_id=f"junk-{index}", name=ATTESTATION_EVENT, entity_type="other",
                entity_id="x", occurred_at=NOW, owner_id="owner:unrelated", payload={},
            ))
    assert snapshot(store).components[0].machines[0].authenticated


def test_rewriting_updated_at_or_rolling_back_fails_closed(opener):
    """Review of #684 (N3a and the rollback probe)."""
    store = opener()
    order_id = _shipped_order(store)
    with store.transaction(owner_id=org.FULFILL) as uow:
        entity = uow.get_entity(ORDER, order_id)
        uow.save_entity(replace(entity, updated_at=NOW + timedelta(days=1)))
    with pytest.raises(ValueError, match="contradicts the stored entity"):
        snapshot(store)

    rolled = opener() if not isinstance(store, MemoryPersistence) else MemoryPersistence()
    _shipped_order(rolled, order_id="order-2")
    receipts = sorted((e for e in rolled.events() if e.name == ATTESTATION_EVENT),
                      key=lambda e: e.payload["to_version"])
    second = receipts[1].payload
    with rolled.transaction(owner_id=org.FULFILL) as uow:
        entity = uow.get_entity(ORDER, "order-2")
        uow.save_entity(replace(entity, version=second["to_version"], state=second["to_state"]))
    with pytest.raises(ValueError, match="contradicts"):
        snapshot(rolled)


def test_a_chain_must_start_at_version_zero():
    """Review of #684 (N3b): a history whose first two steps are gone (no public API
    removes events) and whose third receipt is re-signed without a predecessor, as
    only a key holder could, still fails closed."""
    from hashlib import sha256
    from hmac import new as hmac_new

    from sose.core.transition_authority import _encoded

    store = MemoryPersistence()
    _shipped_order(store)
    events = store._state.events
    receipts = sorted((e for e in events if e.name == ATTESTATION_EVENT),
                      key=lambda e: e.payload["to_version"])
    dropped = {r.causation_id for r in receipts[:2]} | {r.event_id for r in receipts[:2]}
    third = receipts[2]
    claims = {k: v for k, v in third.payload.items() if k != "signature"}
    claims["predecessor_receipt_id"] = None
    resigned = replace(third, payload={
        **claims, "signature": hmac_new(SECRET, _encoded(claims), sha256).hexdigest(),
    })
    store._state.events = [
        resigned if e is third else e for e in events if e.event_id not in dropped
    ]
    with pytest.raises(ValueError, match="does not start at version zero"):
        snapshot(store)


def test_a_missing_entity_of_an_attributed_machine_fails_closed(monkeypatch):
    store = MemoryPersistence()
    _shipped_order(store)
    monkeypatch.setattr(MemoryPersistence, "entity", lambda self, entity_type, entity_id: None)
    with pytest.raises(ValueError, match="has no stored entity"):
        snapshot(store)


def test_the_tick_engine_refuses_an_authority_with_a_domain_warehouse():
    """Review of #684 (N4): a custom runtime whose Engine already has a warehouse."""
    real = builtin_catalog().get("tutorial_job")

    def with_warehouse(*args, **kwargs):
        context, engine = real.build_runtime(*args, **kwargs)
        engine.domain_warehouse = object()
        return context, engine

    job = SimulationJob(
        job_id="tutorial", definition=replace(real, build_runtime=with_warehouse),
        persistence=MemoryPersistence(),
        backend_factory=lambda origin: SimPyBackend(origin=origin), transition_authority=ROOT,
    )
    job.initialize({"random_seed": 3})
    with pytest.raises(ValueError, match="one store"):
        job.run_trigger(trigger_id="t0", triggered_at=NOW)

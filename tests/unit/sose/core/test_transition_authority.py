"""Authentic opt-in Engine transition receipts versus public UoW forgeries (#545)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import TransitionAuthority, ATTESTATION_EVENT
from sose.domain.registry import DomainRegistry, EntityType
from sose.organizational.od2_actor import OrganizationalActor, OrganizationalActorChart
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
OWNER = "engine:verified-actor"
SECRET = b"0123456789abcdef0123456789abcdef"


def engine(store, *, authority):
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=31), scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("organizational_actor", OrganizationalActorChart))
    return Engine(context=context, registry=registry, persistence=store,
                  owner_id=OWNER, transition_authority=authority)


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_real_engine_transition_has_atomic_externally_authenticated_receipt(adapter, tmp_path):
    path = tmp_path / "trusted-engine.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority(issuer_id="engine:trusted", key_id="source:v1", secret=SECRET)
    entity = OrganizationalActor(id="actor:auth", state="admitted", created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(entity)
    command = Command(command_id="cmd:activate", name="activate", entity_type=entity.entity_type,
                      entity_id=entity.id, owner_id=OWNER, due_at=NOW, correlation_id="flow:1")
    try:
        engine(store, authority=root).dispatch(command)
        events = store.events()
        transitions = [e for e in events if e.name == "entity.state_transition"]
        receipts = [e for e in events if e.name == ATTESTATION_EVENT]
        assert len(transitions) == len(receipts) == 1
        with store.transaction(owner_id=OWNER) as uow:
            assert root.verify(uow=uow, event=transitions[0]) == receipts[0]
        assert store.entity(entity.entity_type, entity.id).state == "eligible"

        # An untrusted UoW can append a correctly shaped and ID-derived source
        # event but cannot forge a trusted receipt without the external key.
        forged = replace(transitions[0], event_id=deterministic_id("forged", entity.id))
        with store.transaction(owner_id=OWNER) as uow:
            uow.append_event(forged)
        with store.transaction(owner_id=OWNER) as uow:
            with pytest.raises(ValueError, match="receipt missing"):
                root.verify(uow=uow, event=forged)
        assert store.entity(entity.entity_type, entity.id).state == "eligible"
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            with restored.transaction(owner_id=OWNER) as uow:
                assert root.verify(uow=uow, event=transitions[0]) == receipts[0]
                with pytest.raises(ValueError, match="receipt missing"):
                    root.verify(uow=uow, event=forged)


@pytest.mark.parametrize("mutation", ("mac", "issuer", "event", "duplicate"))
def test_public_writer_cannot_launder_forged_transition_receipt(mutation):
    store = MemoryPersistence()
    root = TransitionAuthority(issuer_id="engine:trusted", key_id="source:v1", secret=SECRET)
    entity = OrganizationalActor(id="actor:auth", state="admitted", created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(entity)
    command = Command(command_id="cmd:activate", name="activate", entity_type=entity.entity_type,
                      entity_id=entity.id, owner_id=OWNER, due_at=NOW)
    engine(store, authority=root).dispatch(command)
    source = next(e for e in store.events() if e.name == "entity.state_transition")
    receipt = next(e for e in store.events() if e.name == ATTESTATION_EVENT)
    with store.transaction(owner_id=OWNER) as uow:
        if mutation == "duplicate":
            uow.append_event(receipt)
        else:
            tampered = dict(receipt.payload)
            if mutation == "mac":
                tampered["signature"] = "0" * 64
            elif mutation == "issuer":
                tampered["issuer_id"] = "foreign"
            else:
                tampered["event_id"] = "forged"
            events = uow._working.events
            events[:] = [replace(x, payload=tampered) if x.event_id == receipt.event_id else x
                         for x in events]
    with store.transaction(owner_id=OWNER) as uow:
        with pytest.raises(ValueError, match="authenticated StateChart receipt"):
            root.verify(uow=uow, event=source)


def test_invalid_transition_is_not_attested_and_signature_key_is_not_data():
    store = MemoryPersistence()
    root = TransitionAuthority(issuer_id="issuer", key_id="key", secret=SECRET)
    item = OrganizationalActor(id="actor:bad", state="admitted", created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(item)
    command = Command(command_id="cmd:exit", name="exit", entity_type=item.entity_type,
                      entity_id=item.id, owner_id=OWNER, due_at=NOW)
    with pytest.raises(Exception):
        engine(store, authority=root).dispatch(command)
    assert not store.events()
    assert store.entity(item.entity_type, item.id).state == "admitted"
    assert SECRET.hex() not in str(store._state)
    with pytest.raises(ValueError, match="256-bit"):
        TransitionAuthority("issuer", "key", b"weak")



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_forged_warehouse_ship_with_matching_entity_version_cannot_gain_trust(
    adapter, tmp_path,
):
    """#545 exact #546 counterexample without depending on closed W4a code."""
    from sose.domain.entity import Entity

    path = tmp_path / "warehouse-fake-transition.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("real-warehouse-engine", "engine-key:v1", SECRET)
    order = Entity(id="warehouse:order", entity_type="warehouse_fulfillment_order",
                   state="requested", created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(order)
    version = order.version + 1
    forged = DomainEvent(
        event_id=deterministic_id(
            "event", OWNER, 5, order.entity_type, order.id, version,
            "ship", "packed", "shipped",
        ),
        name="entity.state_transition", owner_id=OWNER,
        entity_type=order.entity_type, entity_id=order.id,
        occurred_at=NOW, tick=5,
        correlation_id="forged:shipment",
        causation_id="cmd:synthetic-ship",
        payload={"trigger": "ship", "from_state": "packed", "to_state": "shipped"},
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            mutable = uow.get_entity(order.entity_type, order.id)
            mutable.state = "shipped"
            mutable.version = version
            mutable.updated_at = NOW
            uow.save_entity(mutable)
            uow.append_event(forged)
        assert store.entity(order.entity_type, order.id).state == "shipped"
        with store.transaction(owner_id=OWNER) as uow:
            with pytest.raises(ValueError, match="receipt missing"):
                root.verify(uow=uow, event=forged)
        assert not any(x.name == ATTESTATION_EVENT for x in store.events())
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            with recovered.transaction(owner_id=OWNER) as uow:
                with pytest.raises(ValueError, match="receipt missing"):
                    root.verify(uow=uow, event=forged)

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



@pytest.mark.parametrize("mutation", ("event_name", "extra_payload", "same_id_duplicate"))
@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_signed_receipt_does_not_authorize_mutated_or_replayed_source_event(
    mutation, adapter, tmp_path,
):
    """P1: an HMAC must cover every semantic source field and its UoW identity."""
    path = tmp_path / "tampered-signed-source.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("engine:trusted", "source:v1", SECRET)
    entity = OrganizationalActor(id="actor:source-mutation", state="admitted",
                                  created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(entity)
    command = Command(command_id="cmd:activate", name="activate", entity_type=entity.entity_type,
                      entity_id=entity.id, owner_id=OWNER, due_at=NOW)
    try:
        engine(store, authority=root).dispatch(command)
        original = next(e for e in store.events() if e.name == "entity.state_transition")
        with store.transaction(owner_id=OWNER) as uow:
            if mutation == "same_id_duplicate":
                uow.append_event(original)
            else:
                raw = uow._working.events
                for i, event in enumerate(raw):
                    if event.event_id == original.event_id:
                        raw[i] = replace(
                            event,
                            name="unrelated.event" if mutation == "event_name" else event.name,
                            payload={**event.payload, "extra": "attacker"}
                            if mutation == "extra_payload" else event.payload,
                        )
        with store.transaction(owner_id=OWNER) as uow:
            with pytest.raises(ValueError, match="authenticated StateChart"):
                root.verify(uow=uow, event=original)
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            with restored.transaction(owner_id=OWNER) as uow:
                with pytest.raises(ValueError, match="authenticated StateChart"):
                    root.verify(uow=uow, event=original)


@pytest.mark.parametrize("mode", ("direct", "scheduled"))
@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_untrusted_prequeued_events_can_never_be_signed_by_engine(
    mode, adapter, tmp_path,
):
    """P1: event preloaded before recorder output cannot turn Engine into oracle."""
    from sose.core.durable import DurableScheduledItem

    path = tmp_path / "signing-oracle-guard.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("engine:trusted", "source:v1", SECRET)
    actor = OrganizationalActor(id="actor:queue-attack", state="admitted",
                                created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(actor)
    command = Command(command_id="cmd:activate", name="activate",
                      entity_type=actor.entity_type, entity_id=actor.id,
                      owner_id=OWNER, due_at=NOW)
    runtime = engine(store, authority=root)
    forged = DomainEvent(
        event_id="forged:before-dispatch", name="entity.state_transition",
        entity_type=actor.entity_type, entity_id=actor.id,
        occurred_at=NOW, owner_id=OWNER, tick=0, causation_id=command.command_id,
        correlation_id=command.command_id,
        payload={"trigger": "activate", "from_state": "admitted", "to_state": "eligible"},
    )
    if mode == "scheduled":
        work = runtime.scheduler.schedule(command)
    runtime.context.emit(forged)
    try:
        with pytest.raises(ValueError, match="preexisting pending source events"):
            if mode == "direct":
                runtime.dispatch(command)
            else:
                runtime.dispatch_scheduled(DurableScheduledItem(work, command))
        assert not store.events()
        assert store.entity(actor.entity_type, actor.id).state == "admitted"
        if mode == "scheduled":
            assert store.command(command.command_id) == command
    finally:
        if adapter == "sqlite":
            store.close()



@pytest.mark.parametrize("bad_tick", (False, 0.0))
@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_boolean_or_float_tick_cannot_reuse_int_signed_transition(
    bad_tick, adapter, tmp_path,
):
    """Python numeric equality cannot substitute for signed event field types."""
    path = tmp_path / "noncanonical-tick.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("engine:trusted", "key:v1", SECRET)
    actor = OrganizationalActor(id="actor:tick", state="admitted",
                                created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(actor)
    command = Command(command_id="cmd:activate", name="activate",
                      entity_type=actor.entity_type, entity_id=actor.id,
                      owner_id=OWNER, due_at=NOW)
    try:
        engine(store, authority=root).dispatch(command)
        source = next(e for e in store.events() if e.name == "entity.state_transition")
        assert type(source.tick) is int
        assert source.tick == bad_tick  # Numeric equality IS the adversarial trick.
        with store.transaction(owner_id=OWNER) as uow:
            for index, event in enumerate(uow._working.events):
                if event.event_id == source.event_id:
                    uow._working.events[index] = replace(event, tick=bad_tick)
        forged = replace(source, tick=bad_tick)
        with store.transaction(owner_id=OWNER) as uow:
            with pytest.raises(ValueError, match="authenticated StateChart"):
                root.verify(uow=uow, event=forged)
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            with restored.transaction(owner_id=OWNER) as uow:
                with pytest.raises(ValueError, match="authenticated StateChart"):
                    root.verify(uow=uow, event=forged)



@pytest.mark.parametrize("bad_version", (True, 1.0))
@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_signed_transition_rejects_nonstrict_committed_entity_version(
    bad_version, adapter, tmp_path,
):
    """A public writer must not exploit bool/float numeric equality to 1."""
    path = tmp_path / "bad-committed-entity-version.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("engine:trusted", "key:v1", SECRET)
    actor = OrganizationalActor(
        id="actor:bad-version", state="admitted", created_at=NOW, updated_at=NOW,
    )
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(actor)
    command = Command(
        command_id="cmd:activate", name="activate",
        entity_type=actor.entity_type, entity_id=actor.id,
        owner_id=OWNER, due_at=NOW,
    )
    try:
        engine(store, authority=root).dispatch(command)
        signed = next(x for x in store.events() if x.name == "entity.state_transition")
        with store.transaction(owner_id=OWNER) as uow:
            corrupted = uow.get_entity(actor.entity_type, actor.id)
            assert corrupted.version == 1
            corrupted.version = bad_version
            uow.save_entity(corrupted)
        with store.transaction(owner_id=OWNER) as uow:
            with pytest.raises(ValueError, match="authenticated StateChart receipt lacks committed entity version"):
                root.verify(uow=uow, event=signed)
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            with restored.transaction(owner_id=OWNER) as uow:
                with pytest.raises(ValueError, match="authenticated StateChart receipt lacks committed entity version"):
                    root.verify(uow=uow, event=signed)


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("mutation", (
    "wrong_target", "wrong_target_type", "future_due",
    "future_issued", "naive_due", "naive_issued",
    "naive_event", "naive_before", "backdated_before",
))
def test_signer_rejects_noncausal_command_envelopes(
    adapter, mutation, tmp_path,
):
    """#545: a valid StateChart event cannot authorize another target/time."""
    path = tmp_path / "command-envelope.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("engine:trusted", "source:v1", SECRET)
    actor = OrganizationalActor(
        id="actor:envelope", state="admitted", created_at=NOW, updated_at=NOW,
    )
    original = Command(
        command_id="cmd:envelope", name="activate",
        entity_type=actor.entity_type, entity_id=actor.id,
        owner_id=OWNER, due_at=NOW, issued_at=NOW,
    )
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(actor)
    try:
        engine(store, authority=root).dispatch(original)
        source = next(event for event in store.events()
                      if event.name == "entity.state_transition")
        receipt = next(event for event in store.events()
                       if event.name == ATTESTATION_EVENT)
        after = store.entity(actor.entity_type, actor.id)
        assert root.seal(before=actor, after=after,
                         event=source, command=original) == receipt

        bad_command = original
        bad_before = actor
        bad_source = source
        if mutation == "wrong_target":
            bad_command = replace(original, entity_id="actor:another")
        elif mutation == "wrong_target_type":
            bad_command = replace(original, entity_type="other_actor_type")
        elif mutation == "future_due":
            bad_command = replace(original, due_at=NOW + timedelta(microseconds=1))
        elif mutation == "future_issued":
            bad_command = replace(original, issued_at=NOW + timedelta(microseconds=1))
        elif mutation == "naive_due":
            bad_command = replace(original, due_at=NOW.replace(tzinfo=None))
        elif mutation == "naive_issued":
            bad_command = replace(original, issued_at=NOW.replace(tzinfo=None))
        elif mutation == "naive_event":
            bad_source = replace(source, occurred_at=NOW.replace(tzinfo=None))
        elif mutation == "naive_before":
            bad_before = replace(actor, updated_at=NOW.replace(tzinfo=None))
        elif mutation == "backdated_before":
            bad_before = replace(actor, updated_at=NOW + timedelta(microseconds=1))
        with pytest.raises(ValueError, match="Engine attestation"):
            root.seal(before=bad_before, after=after,
                      event=bad_source, command=bad_command)
        assert len(store.events()) == 2
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            with reopened.transaction(owner_id=OWNER) as uow:
                assert root.verify(uow=uow, event=source) == receipt


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_attested_early_dispatch_rolls_back_atomically(adapter, tmp_path):
    """Dispatch cannot sign a future Command and must leave no entity/event."""
    path = tmp_path / "premature-dispatch.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("engine:trusted", "source:v1", SECRET)
    actor = OrganizationalActor(
        id="actor:premature", state="admitted", created_at=NOW, updated_at=NOW,
    )
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(actor)
    command = Command(
        command_id="cmd:future", name="activate",
        entity_type=actor.entity_type, entity_id=actor.id,
        owner_id=OWNER, due_at=NOW + timedelta(microseconds=1),
    )
    try:
        with pytest.raises(ValueError, match="Engine attestation"):
            engine(store, authority=root).dispatch(command)
        persisted = store.entity(actor.entity_type, actor.id)
        assert persisted.state == "admitted" and persisted.version == 0
        assert not store.events()
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert reopened.entity(actor.entity_type, actor.id).state == "admitted"
            assert not reopened.events()

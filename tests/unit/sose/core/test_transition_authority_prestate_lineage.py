"""#545 RED: public UoW predecessor forgery must not become trusted Engine evidence.

These regressions fail on implementations that permit an issuer to sign an
otherwise native transition from a forged or incomplete predecessor history.
Neither a correctly signed successor nor a deterministic event ID is evidence
that the preceding committed entity state was reached by a real StateChart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


OWNER = "warehouse:attested-prestate"
NOW = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
KEY = b"0123456789abcdef0123456789abcdef"


def _engine(store):
    registry = DomainRegistry()
    registry.register(EntityType("warehouse_fulfillment_order", FulfillmentOrderChart))
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=17),
        scheduler=Scheduler(),
    )
    return Engine(
        persistence=store, owner_id=OWNER, registry=registry, context=context,
        transition_authority=TransitionAuthority("warehouse:issuer", "key:v1", KEY),
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_unsigned_public_prestate_cannot_be_laundered_by_real_engine_ship(adapter, tmp_path):
    """An attacker may write packed, but Engine must not certify a forged history."""
    path = tmp_path / "prestate-forgery.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    order = FulfillmentOrder(
        id="order:forged-prestate", state="requested",
        created_at=NOW, updated_at=NOW,
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)

        # Public writer forges the prerequisite packed state and plausible
        # version without executing allocate, start_pick, or pack.
        with store.transaction(owner_id=OWNER) as uow:
            tampered = uow.get_entity(order.entity_type, order.id)
            tampered.state = "packed"
            tampered.version = 3
            tampered.updated_at = NOW
            uow.save_entity(tampered)
        assert not [e for e in store.events() if e.name == ATTESTATION_EVENT]

        command = Command(
            command_id="cmd:ship-forged-prestate", name="ship",
            entity_type=order.entity_type, entity_id=order.id,
            owner_id=OWNER, due_at=NOW, issued_at=NOW,
            correlation_id="flow:forged-prestate",
        )
        with pytest.raises(ValueError, match="trusted predecessor|unauthenticated prestate"):
            _engine(store).dispatch(command)

        # Neither an untrusted packed state nor an untrusted predecessor
        # may induce an issuer-signed shipped state.
        assert store.entity(order.entity_type, order.id).state == "packed"
        assert not store.events()
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_legitimate_four_step_warehouse_path_remains_attestable(adapter, tmp_path):
    """Positive control: independent source receipts may be verified after replay."""
    path = tmp_path / "prestate-genuine.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    order = FulfillmentOrder(
        id="order:genuine", state="requested", created_at=NOW, updated_at=NOW,
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)
        runtime = _engine(store)
        for step in ("allocate", "start_pick", "pack", "ship"):
            runtime.dispatch(Command(
                command_id=f"cmd:{step}-genuine", name=step,
                entity_type=order.entity_type, entity_id=order.id,
                owner_id=OWNER, due_at=NOW, issued_at=NOW,
                correlation_id="flow:genuine",
            ))
        sources = [e for e in store.events() if e.name == "entity.state_transition"]
        assert len(sources) == 4
        with store.transaction(owner_id=OWNER) as uow:
            authority = TransitionAuthority("warehouse:issuer", "key:v1", KEY)
            for source in sources:
                assert authority.verify(uow=uow, event=source).name == ATTESTATION_EVENT
        assert store.entity(order.entity_type, order.id).state == "shipped"
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_signed_final_predecessor_cannot_hide_missing_earlier_history(adapter, tmp_path):
    """A historical signed 'pack' alone cannot legitimize forged version 1/2.

    A legitimate issuer may have signed an intermediate transition in an
    older unfenced runtime. The issuer signature is genuine, but cannot
    establish a complete provenance chain retroactively.
    """
    from sose.core.events import DomainEvent
    from sose.core.identity import deterministic_id
    from dataclasses import replace

    path = tmp_path / "prestate-incomplete-signed-history.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    root = TransitionAuthority("warehouse:issuer", "key:v1", KEY)
    order = FulfillmentOrder(
        id="order:signed-but-incomplete", state="requested",
        created_at=NOW, updated_at=NOW,
    )
    cmd = Command(
        command_id="cmd:pack-with-unproven-chain", name="pack",
        entity_type=order.entity_type, entity_id=order.id,
        owner_id=OWNER, due_at=NOW, issued_at=NOW,
        correlation_id="flow:unproven-chain",
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)

        # Fabricated steps 1 and 2 have no trusted issuer evidence.
        picking = replace(order, state="picking", version=2)
        packed = replace(order, state="packed", version=3)
        pack_event = DomainEvent(
            event_id=deterministic_id(
                "event", OWNER, 0, order.entity_type, order.id,
                3, "pack", "picking", "packed",
            ),
            name="entity.state_transition", owner_id=OWNER,
            entity_type=order.entity_type, entity_id=order.id,
            occurred_at=NOW, tick=0,
            correlation_id=cmd.correlation_id,
            causation_id=cmd.command_id,
            payload={
                "trigger": "pack",
                "from_state": "picking",
                "to_state": "packed",
            },
        )
        signed_pack = root.seal(
            before=picking, after=packed, event=pack_event, command=cmd,
        )
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(packed)
            uow.append_event(pack_event)
            uow.append_event(signed_pack)
        with store.transaction(owner_id=OWNER) as uow:
            assert root.verify(uow=uow, event=pack_event) == signed_pack

        with pytest.raises(ValueError, match="trusted predecessor"):
            _engine(store).dispatch(Command(
                command_id="cmd:ship-with-historical-gap", name="ship",
                entity_type=order.entity_type, entity_id=order.id,
                owner_id=OWNER, due_at=NOW, issued_at=NOW,
                correlation_id="flow:unproven-chain",
            ))
        assert store.entity(order.entity_type, order.id).state == "packed"
        assert not any(
            event.payload.get("trigger") == "ship"
            for event in store.events() if event.name == "entity.state_transition"
        )
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("tamper", ("state", "updated_at"))
def test_trusted_prestate_rejects_modified_projection_after_authentic_allocate(
    adapter, tamper, tmp_path,
):
    """#545: signed history must match the *current durable* predecessor.

    An issuer-authenticated first transition must not authorize a later
    native transition when a public UoW subsequently rewrites its projection.
    """
    path = tmp_path / "signed-predecessor-projection.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    order = FulfillmentOrder(
        id="order:tampered-signed-predecessor", state="requested",
        created_at=NOW, updated_at=NOW,
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)
        runtime = _engine(store)
        runtime.dispatch(Command(
            command_id="cmd:first-authentic-allocate", name="allocate",
            entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
            due_at=NOW, issued_at=NOW,
            correlation_id="flow:tampered-signed-predecessor",
        ))
        first_transition = store.entity(order.entity_type, order.id)
        assert first_transition.version == 1
        with store.transaction(owner_id=OWNER) as uow:
            corrupted = uow.get_entity(order.entity_type, order.id)
            if tamper == "state":
                corrupted.state = "requested"
            else:
                corrupted.updated_at = NOW + timedelta(seconds=7)
            uow.save_entity(corrupted)

        recorded_before = tuple(store.events())
        assert len(recorded_before) == 2  # native transition + protected receipt
        # A state rewrite is rejected by the authenticated receipt before
        # the final chain/projection comparison. A timestamp rewrite reaches
        # the latter comparison; both must leave the UoW fully rolled back.
        expected_rejection = (
            "authenticated StateChart receipt contradicts terminal projection"
            if tamper == "state" else "trusted predecessor chain contradicts"
        )
        with pytest.raises(ValueError, match=expected_rejection):
            runtime.dispatch(Command(
                command_id="cmd:after-public-projection-corruption",
                name="allocate" if tamper == "state" else "start_pick",
                entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
                due_at=NOW, issued_at=NOW,
                correlation_id="flow:tampered-signed-predecessor",
            ))
        assert tuple(store.events()) == recorded_before
        assert store.entity(order.entity_type, order.id).version == 1
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_trusted_prestate_rejects_version_zero_rewind_with_signed_history(
    adapter, tmp_path,
):
    """A public writer cannot reset version/state and replay an initial transition."""
    path = tmp_path / "signed-version-rewind.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    order = FulfillmentOrder(
        id="order:rewound-signed-history", state="requested",
        created_at=NOW, updated_at=NOW,
    )
    try:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)
        runtime = _engine(store)
        runtime.dispatch(Command(
            command_id="cmd:allocate-before-rewind", name="allocate",
            entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
            due_at=NOW, issued_at=NOW,
            correlation_id="flow:rewound-signed-history",
        ))
        with store.transaction(owner_id=OWNER) as uow:
            rewound = uow.get_entity(order.entity_type, order.id)
            rewound.version = 0
            rewound.state = "requested"
            rewound.updated_at = NOW
            uow.save_entity(rewound)
        recorded_before = tuple(store.events())
        with pytest.raises(ValueError, match="version-zero entity"):
            runtime.dispatch(Command(
                command_id="cmd:allocate-after-rewind", name="allocate",
                entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
                due_at=NOW, issued_at=NOW,
                correlation_id="flow:rewound-signed-history",
            ))
        assert tuple(store.events()) == recorded_before
    finally:
        if adapter == "sqlite":
            store.close()

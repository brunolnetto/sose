"""#545 review: chained attested predecessor checks with owner-independent receipt ids.

Each Engine receipt signs its predecessor receipt id and a digest of the
entity attributes. The dispatch-time prestate check reads only the current
and next receipt by deterministic id, so it never scans the event log.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.durable import DurableScheduledItem
from sose.core.engine import Engine
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.persistence.memory import MemoryPersistence
from sose.persistence.memory_uow_core import CoreRecordsUnitOfWorkMixin
from sose.persistence.sqlite import SQLitePersistence


OWNER = "warehouse:attested-chain"
FOREIGN = "warehouse:foreign-owner"
NOW = datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
KEY = b"0123456789abcdef0123456789abcdef"
ROOT = TransitionAuthority("warehouse:issuer", "key:v1", KEY)


def _engine(store, owner=OWNER):
    registry = DomainRegistry()
    registry.register(EntityType("warehouse_fulfillment_order", FulfillmentOrderChart))
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=17),
        scheduler=Scheduler(),
    )
    return Engine(
        persistence=store, owner_id=owner, registry=registry, context=context,
        transition_authority=ROOT,
    )


def _command(step, order_id, owner=OWNER, suffix=""):
    return Command(
        command_id=f"cmd:{step}{suffix}", name=step,
        entity_type="warehouse_fulfillment_order", entity_id=order_id,
        owner_id=owner, due_at=NOW, issued_at=NOW, correlation_id="flow:chain",
    )


def _transition(order, owner, version, trigger, from_state, to_state, command):
    return DomainEvent(
        event_id=deterministic_id(
            "event", owner, 0, order.entity_type, order.id,
            version, trigger, from_state, to_state,
        ),
        name="entity.state_transition", owner_id=owner,
        entity_type=order.entity_type, entity_id=order.id,
        occurred_at=NOW, tick=0, correlation_id=command.correlation_id,
        causation_id=command.command_id,
        payload={"trigger": trigger, "from_state": from_state, "to_state": to_state},
    )


@pytest.fixture(params=("memory", "sqlite"))
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryPersistence()
        return
    persistence = SQLitePersistence(tmp_path / "chain.sqlite")
    yield persistence
    persistence.close()


def _seed(store, order_id="order:chain"):
    order = FulfillmentOrder(id=order_id, state="requested", created_at=NOW, updated_at=NOW)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(order)
    return order


def _rewrite(store, order, **changes):
    with store.transaction(owner_id=OWNER) as uow:
        entity = uow.get_entity(order.entity_type, order.id)
        for name, value in changes.items():
            setattr(entity, name, value)
        uow.save_entity(entity)


def _rejects(store, order, step, match):
    recorded = tuple(store.events())
    with pytest.raises(ValueError, match=match):
        _engine(store).dispatch(_command(step, order.id, suffix=":rejected"))
    assert tuple(store.events()) == recorded


@pytest.mark.parametrize("mode", ("direct", "scheduled"))
def test_attested_dispatch_never_scans_the_event_log(store, mode, monkeypatch):
    order = _seed(store)
    runtime = _engine(store)
    runtime.dispatch(_command("allocate", order.id))
    runtime.dispatch(_command("start_pick", order.id))

    def no_scan(self):
        raise AssertionError("attested dispatch scanned the full event log")

    monkeypatch.setattr(CoreRecordsUnitOfWorkMixin, "events", no_scan)
    command = _command("pack", order.id)
    if mode == "direct":
        runtime.dispatch(command)
    else:
        work = runtime.scheduler.schedule(command)
        runtime.dispatch_scheduled(DurableScheduledItem(work, command))
    monkeypatch.undo()

    assert store.entity(order.entity_type, order.id).state == "packed"
    sources = [e for e in store.events() if e.name == "entity.state_transition"]
    receipts = [e for e in store.events() if e.name == ATTESTATION_EVENT]
    assert len(sources) == len(receipts) == 3
    links = [r.payload["predecessor_receipt_id"] for r in receipts]
    assert links == [None, receipts[0].event_id, receipts[1].event_id]
    with store.transaction(owner_id=OWNER) as uow:
        for source in sources:
            assert ROOT.verify(uow=uow, event=source).name == ATTESTATION_EVENT


def test_forged_attributes_cannot_pass_as_signed_projection(store):
    order = _seed(store)
    _engine(store).dispatch(_command("allocate", order.id))
    source = next(e for e in store.events() if e.name == "entity.state_transition")
    _rewrite(store, order, attributes={"priority": "forged-express"})

    _rejects(store, order, "start_pick", "contradicts terminal projection")
    with store.transaction(owner_id=OWNER) as uow:
        with pytest.raises(ValueError, match="contradicts terminal projection"):
            ROOT.verify(uow=uow, event=source)


def test_foreign_owner_transition_event_does_not_block_this_owner(store):
    order = _seed(store)
    runtime = _engine(store)
    runtime.dispatch(_command("allocate", order.id))
    foreign = DomainEvent(
        event_id="foreign:transition", name="entity.state_transition",
        entity_type=order.entity_type, entity_id=order.id,
        occurred_at=NOW, tick=0, owner_id=FOREIGN,
        payload={"trigger": "allocate", "from_state": "requested", "to_state": "allocated"},
    )
    with store.transaction(owner_id=FOREIGN) as uow:
        uow.append_event(foreign)

    runtime.dispatch(_command("start_pick", order.id))
    assert store.entity(order.entity_type, order.id).state == "picking"


def test_foreign_owner_receipt_cannot_anchor_this_owner(store):
    order = _seed(store)
    command = _command("allocate", order.id, owner=FOREIGN)
    after = replace(order, state="allocated", version=1)
    event = _transition(order, FOREIGN, 1, "allocate", "requested", "allocated", command)
    receipt = ROOT.seal(before=order, after=after, event=event, command=command)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(after)
    with store.transaction(owner_id=FOREIGN) as uow:
        uow.append_event(event)
        uow.append_event(receipt)

    _rejects(store, order, "start_pick", "trusted predecessor has foreign owner")


def test_replayed_version_after_rewind_is_rejected(store):
    order = _seed(store)
    runtime = _engine(store)
    runtime.dispatch(_command("allocate", order.id))
    allocated = store.entity(order.entity_type, order.id)
    runtime.dispatch(_command("start_pick", order.id))
    # Rewind to the exact, genuinely signed version-1 projection.
    _rewrite(
        store, order, state=allocated.state, version=allocated.version,
        updated_at=allocated.updated_at, attributes=allocated.attributes,
    )
    _rejects(store, order, "start_pick", "trusted predecessor version replayed")


def test_unlinked_signed_receipt_reports_missing_version(store):
    order = _seed(store)
    _engine(store).dispatch(_command("allocate", order.id))
    allocated = store.entity(order.entity_type, order.id)
    picking = replace(allocated, state="picking", version=2)
    command = _command("start_pick", order.id, suffix=":unlinked")
    event = _transition(order, OWNER, 2, "start_pick", "allocated", "picking", command)
    # The key holder signs version 2 without the validated predecessor link.
    receipt = ROOT.seal(before=allocated, after=picking, event=event, command=command)
    assert receipt.payload["predecessor_receipt_id"] is None
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(picking)
        uow.append_event(event)
        uow.append_event(receipt)

    _rejects(store, order, "pack", "trusted predecessor chain has missing version")


def test_version_one_receipt_must_start_at_initial_state(store):
    order = _seed(store)
    picking = replace(order, state="picking")
    allocated = replace(order, state="allocated", version=1)
    command = _command("allocate", order.id, suffix=":wrong-root")
    event = _transition(order, OWNER, 1, "allocate", "picking", "allocated", command)
    receipt = ROOT.seal(before=picking, after=allocated, event=event, command=command)
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(allocated)
        uow.append_event(event)
        uow.append_event(receipt)

    _rejects(store, order, "start_pick", "does not start at the initial state")


def test_timestamp_regression_of_projection_is_rejected(store):
    order = _seed(store)
    _engine(store).dispatch(_command("allocate", order.id))
    _rewrite(store, order, updated_at=NOW - timedelta(seconds=1))
    _rejects(store, order, "start_pick", "trusted predecessor chain contradicts")


def test_seal_refuses_mismatched_predecessor_link(store):
    order = _seed(store)
    runtime = _engine(store)
    runtime.dispatch(_command("allocate", order.id))
    runtime.dispatch(_command("start_pick", order.id))
    first, second = [e for e in store.events() if e.name == ATTESTATION_EVENT]
    picking = store.entity(order.entity_type, order.id)
    packed = replace(picking, state="packed", version=3)
    command = _command("pack", order.id)
    event = _transition(order, OWNER, 3, "pack", "picking", "packed", command)
    with pytest.raises(ValueError, match="predecessor receipt does not match"):
        ROOT.seal(before=picking, after=packed, event=event, command=command,
                  predecessor=first)
    linked = ROOT.seal(before=picking, after=packed, event=event, command=command,
                       predecessor=second)
    assert linked.payload["predecessor_receipt_id"] == second.event_id


def _rollback_to_requested(store, order):
    _rewrite(store, order, state="requested", version=0, updated_at=NOW, attributes={})


def test_other_owner_cannot_fork_history_after_projection_rollback(store):
    """Same authority, two owners: B must not sign v1 over A's v1..v3."""
    order = _seed(store)
    runtime = _engine(store)
    for step in ("allocate", "start_pick", "pack"):
        runtime.dispatch(_command(step, order.id))
    _rollback_to_requested(store, order)
    recorded = tuple(store.events())
    with pytest.raises(ValueError, match="version-zero entity"):
        _engine(store, owner=FOREIGN).dispatch(_command("allocate", order.id, owner=FOREIGN))
    assert tuple(store.events()) == recorded
    assert store.entity(order.entity_type, order.id).version == 0


@pytest.mark.parametrize("version", (0, 1))
def test_junk_event_at_next_receipt_id_fails_closed(store, version):
    """Accepted DoS: a public writer can block, but never advance, the chain."""
    order = _seed(store)
    if version:
        _engine(store).dispatch(_command("allocate", order.id))
    junk = DomainEvent(
        event_id=deterministic_id(
            "engine-attested-transition", order.entity_type, order.id, version + 1,
        ),
        name="public.junk", entity_type=order.entity_type, entity_id=order.id,
        occurred_at=NOW, tick=0, owner_id=OWNER, payload={},
    )
    with store.transaction(owner_id=OWNER) as uow:
        uow.append_event(junk)
    before = store.entity(order.entity_type, order.id)
    step = "start_pick" if version else "allocate"
    _rejects(store, order, step, "version-zero entity|trusted predecessor version replayed")
    assert store.entity(order.entity_type, order.id) == before

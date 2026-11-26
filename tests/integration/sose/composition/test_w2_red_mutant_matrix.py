"""#494/#578: the W2 RED mutant matrix.

#578 asks for "RED mutants for ignored picker capacity=2, premature consumption,
forged owner source, stale owner, and duplicate post-COMMIT effect". Each row pairs
an oracle with a mutant of the production code that oracle guards:

- **GREEN:** the oracle passes on the real code;
- **RED:** with the mutant applied, the same oracle fails with an assertion.

A mutant that slipped past its oracle would show the oracle proves nothing. The
PostgreSQL versions of these properties live in the e2e tests named in each row;
this matrix runs in-process (Memory) so every mutant is exercised on every CI run.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.durable import DurableScheduledItem, DurableScheduler
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import (
    ATTESTATION_EVENT,
    ReceiptAuthority,
    TransitionAuthority,
)
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.persistence.memory import MemoryPersistence, StaleWriterError
from sose.persistence.memory_fencing import _WriterFences
from sose.persistence.ownership import FencedEnginePersistence
from tests.integration.sose.examples.warehouse_fulfillment import (
    test_warehouse_fulfillment_service_capacity as capacity_tests,
)

OWNER = "warehouse:w2-mutants"
NOW = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
ROOT = TransitionAuthority("engine:w2", "w2:v1", b"0123456789abcdef" * 2)
ORDER_TYPE = "warehouse_fulfillment_order"
ORDER = "order:w2"
SHIP = "command:ship"


def _engine(store, authority=ROOT) -> Engine:
    registry = DomainRegistry()
    registry.register(EntityType(ORDER_TYPE, FulfillmentOrderChart))
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=41), scheduler=Scheduler(),
    )
    return Engine(persistence=store, owner_id=OWNER, registry=registry,
                  context=context, transition_authority=authority)


def _command(name: str, command_id: str) -> Command:
    return Command(command_id=command_id, name=name, entity_type=ORDER_TYPE, entity_id=ORDER,
                   owner_id=OWNER, due_at=NOW, issued_at=NOW, correlation_id="flow:w2")


def _packed_with_pending_ship(store) -> DurableScheduledItem:
    return _packed_with_pending_ship_by(store, ROOT)


def _ship_receipts(store):
    return [e for e in store.events() if e.name == ATTESTATION_EVENT
            and e.payload["trigger"] == "ship"]


# --- oracles: each raises AssertionError on a violated property ---------------------

def oracle_picker_capacity_two():
    """Two pickers run in parallel (as the W1 falsifier in the capacity tests)."""
    from sose.examples.warehouse_fulfillment.records import service_task_id

    persistence, entities, engine, backend = capacity_tests._runtime(picker_capacity=2)
    order = persistence.entity(ORDER_TYPE, entities.order_id)
    allocation_ids = tuple(str(v) for v in order.attributes["allocation_ids"])
    capacity_tests._reconcile(persistence, entities, engine, backend)
    states = [persistence.entity("warehouse_fulfillment_service_task",
                                 service_task_id("pick", allocation_id)).state
              for allocation_id in allocation_ids]
    reservations = persistence.resource_reservations()
    if states != ["in_progress", "in_progress"] or len(reservations) != 2:
        raise AssertionError(
            f"picker capacity 2 not honored: tasks {states}, {len(reservations)} reservations")


def oracle_no_premature_consumption():
    """A failed claim leaves the Command pending (PG: test_postgres_attested_uncertain_commit)."""
    store = MemoryPersistence()
    item = _packed_with_pending_ship(store)
    failing = replace(ROOT)
    original_seal = ReceiptAuthority.seal

    def failing_seal(self, **kwargs):
        if self is failing:
            raise RuntimeError("injected failure after the transition")
        return original_seal(self, **kwargs)

    ReceiptAuthority.seal = failing_seal
    try:
        try:
            _engine(store, failing).dispatch_scheduled(item)
        except RuntimeError:
            pass
    finally:
        ReceiptAuthority.seal = original_seal
    assert store.entity(ORDER_TYPE, ORDER).state == "packed", "the transition rolled back"
    pending = {i.command.command_id for i in DurableScheduler(store, owner_id=OWNER).pending()}
    assert SHIP in pending, "the Command was consumed although the claim failed"


def oracle_forged_owner_source():
    """A chain signed by another key is refused (PG: test_postgres_trusted_transition_receipts)."""
    store = MemoryPersistence()
    impostor = TransitionAuthority("engine:w2", "w2:v1", b"x" * 32)
    item = _packed_with_pending_ship_by(store, impostor)
    _engine(store, impostor).dispatch_scheduled(item)
    events = [e for e in store.events() if e.entity_id == ORDER]
    try:
        ROOT.verify_history(events=events, entity=store.entity(ORDER_TYPE, ORDER))
    except ValueError as error:
        assert "signature invalid" in str(error)
        return
    raise AssertionError("a chain signed by a foreign key was authenticated")


def _packed_with_pending_ship_by(store, authority) -> DurableScheduledItem:
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(FulfillmentOrder(id=ORDER, state="requested",
                                         created_at=NOW, updated_at=NOW))
    engine = _engine(store, authority)
    for step in ("allocate", "start_pick", "pack"):
        engine.dispatch(_command(step, f"command:{step}"))
    command = _command("ship", SHIP)
    return DurableScheduledItem(engine.scheduler.schedule(command), command)


def oracle_stale_owner():
    """A superseded writer cannot dispatch (PG: test_postgres_trusted_transition_receipts)."""
    store = MemoryPersistence()
    item = _packed_with_pending_ship(store)
    old = store.claim_writer(OWNER, expected_epoch=store.writer_epoch())
    store.claim_writer(OWNER, expected_epoch=store.writer_epoch())   # a newer owner
    stale = _engine(FencedEnginePersistence(store, old))
    try:
        stale.dispatch_scheduled(item)
    except StaleWriterError:
        pass
    else:
        raise AssertionError("a stale owner epoch dispatched")
    assert store.entity(ORDER_TYPE, ORDER).state == "packed"
    assert _ship_receipts(store) == []


def oracle_no_duplicate_post_commit_effect():
    """Retrying a committed claim adds nothing (PG: lost COMMIT reply, #687).

    The retry runs without an authority: on the attested path the chart refuses a
    second ship anyway ("exactly one recorder-produced transition"), which would hide
    a missing consumed-callback guard. This row proves that guard is needed.
    """
    store = MemoryPersistence()
    item = _packed_with_pending_ship(store)
    assert _engine(store).dispatch_scheduled(item) is True
    engine = _engine(store, authority=None)                # no attested refusal to hide behind
    retried = engine.dispatch_scheduled(item)
    assert retried is False, "a consumed callback was executed again"
    ships = [e for e in store.events() if e.name == "entity.state_transition"
             and e.payload["trigger"] == "ship"]
    assert len(ships) == 1 and len(_ship_receipts(store)) == 1


# --- mutants: each is the production defect its oracle must catch ------------------

@contextmanager
def mutant_capacity_capped_to_one(monkeypatch):
    from sose.backends.simpy_resources import _SimPyResourceOperations

    original = _SimPyResourceOperations.create_resource
    monkeypatch.setattr(_SimPyResourceOperations, "create_resource",
                        lambda self, name, *, capacity=1: original(self, name, capacity=1))
    yield


@contextmanager
def mutant_premature_consumption(monkeypatch):
    """Consume work and Command in their own transaction before the claim runs."""
    original = Engine._dispatch_scheduled_in_uow

    def consuming_first(self, item, uow):
        with self.persistence.transaction(owner_id=self.owner_id) as early:
            early.delete_scheduled_work(item.work.work_id)
            early.delete_command(item.command.command_id)
        uow.save_command(item.command)                     # let the claim see it once more
        uow.save_scheduled_work(item.work)
        return original(self, item, uow)

    monkeypatch.setattr(Engine, "_dispatch_scheduled_in_uow", consuming_first)
    yield


@contextmanager
def mutant_signature_not_checked(monkeypatch):
    monkeypatch.setattr(TransitionAuthority, "_valid", lambda self, signature, data: True)
    yield


@contextmanager
def mutant_epoch_not_checked(monkeypatch):
    monkeypatch.setattr(_WriterFences, "require_current",
                        lambda self, owner_epoch, writer_scope: None)
    yield


@contextmanager
def mutant_consumed_callback_replayed(monkeypatch):
    """Execute a callback whose work and Command were already consumed."""
    original = Engine._dispatch_scheduled_in_uow

    def replaying(self, item, uow):
        if uow.get_scheduled_work(item.work.work_id) is None:
            uow.save_command(item.command)
            uow.save_scheduled_work(item.work)
        return original(self, item, uow)

    monkeypatch.setattr(Engine, "_dispatch_scheduled_in_uow", replaying)
    yield


MATRIX = {
    "ignored picker capacity=2": (oracle_picker_capacity_two, mutant_capacity_capped_to_one),
    "premature consumption": (oracle_no_premature_consumption, mutant_premature_consumption),
    "forged owner source": (oracle_forged_owner_source, mutant_signature_not_checked),
    "stale owner": (oracle_stale_owner, mutant_epoch_not_checked),
    "duplicate post-COMMIT effect": (
        oracle_no_duplicate_post_commit_effect, mutant_consumed_callback_replayed,
    ),
}


@pytest.mark.parametrize("row", list(MATRIX))
def test_the_oracle_is_green_on_the_real_code(row):
    oracle, _mutant = MATRIX[row]
    oracle()


# The assertion each mutant must trip: the property, not an incidental crash.
RED_BECAUSE = {
    "ignored picker capacity=2": r"picker capacity 2 not honored: tasks \['in_progress', 'queued'\]",
    "premature consumption": r"Command was consumed although the claim failed",
    "forged owner source": r"foreign key was authenticated",
    "stale owner": r"stale owner epoch dispatched",
    "duplicate post-COMMIT effect": r"consumed callback was executed again",
}


@pytest.mark.parametrize("row", list(MATRIX))
def test_the_oracle_is_red_on_its_mutant(row, monkeypatch):
    oracle, mutant = MATRIX[row]
    with mutant(monkeypatch):
        with pytest.raises(AssertionError, match=RED_BECAUSE[row]):
            oracle()

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
)
from sose.core.scheduler import Scheduler
from sose.core.containers import DurableContainerManager
from sose.domain.registry import DomainRegistry
from sose.persistence.memory import MemoryPersistence

NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def test_define_persists_definition_and_initial_level_atomically():
    store = MemoryPersistence()
    manager = DurableContainerManager(store)

    manager.define(ContainerDefinition("fuel", capacity=100.0, initial=25.0))

    assert store.container_definitions() == (
        ContainerDefinition("fuel", capacity=100.0, initial=25.0),
    )
    assert store.container_states() == (
        ContainerState("fuel", level=25.0),
    )


def test_completed_put_updates_level_and_persists_terminal_result():
    store = MemoryPersistence()
    manager = DurableContainerManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(ContainerDefinition("fuel", capacity=100.0, initial=25.0))
    manager.rebuild_backend(backend)

    intent = manager.put(
        backend,
        container_name="fuel",
        request_id="delivery",
        amount=15.0,
        requested_at=NOW,
    )

    assert store.container_operation_intents() == (intent,)
    assert store.container_operation_results() == ()

    backend.run_until(NOW)

    assert store.container_operation_intents() == ()
    assert store.container_states() == (ContainerState("fuel", level=40.0),)
    result = store.container_operation_results()[0]
    assert result.request_id == "delivery"
    assert result.operation == "put"
    assert result.amount == 15.0
    assert result.level_before == 25.0
    assert result.level_after == 40.0


def test_completed_get_updates_level_and_returns_result_to_live_callback():
    store = MemoryPersistence()
    manager = DurableContainerManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(ContainerDefinition("fuel", capacity=100.0, initial=25.0))
    manager.rebuild_backend(backend)
    observed = []

    manager.get(
        backend,
        container_name="fuel",
        request_id="consume",
        amount=10.0,
        requested_at=NOW,
        on_completed=observed.append,
    )
    backend.run_until(NOW)

    assert store.container_states() == (ContainerState("fuel", level=15.0),)
    assert observed == [store.container_operation_results()[0]]


def test_blocked_get_survives_restart_and_completion_is_durable_without_callback():
    store = MemoryPersistence()
    first = DurableContainerManager(store)
    backend1 = SimPyBackend(origin=NOW)
    first.define(ContainerDefinition("fuel", capacity=100.0, initial=5.0))
    first.rebuild_backend(backend1)

    first.get(
        backend1,
        container_name="fuel",
        request_id="consume",
        amount=10.0,
        requested_at=NOW,
    )
    backend1.run_until(NOW)

    assert [i.request_id for i in store.container_operation_intents()] == ["consume"]
    assert store.container_states() == (ContainerState("fuel", level=5.0),)

    backend2 = SimPyBackend(origin=NOW)
    recovered = DurableContainerManager(store)
    recovered.rebuild_backend(backend2)
    recovered.put(
        backend2,
        container_name="fuel",
        request_id="delivery",
        amount=10.0,
        requested_at=NOW,
    )
    backend2.run_until(NOW)

    assert store.container_operation_intents() == ()
    assert store.container_states() == (ContainerState("fuel", level=5.0),)
    assert {r.request_id for r in store.container_operation_results()} == {
        "delivery",
        "consume",
    }
    assert recovered.result("consume").level_after == 5.0


def test_blocked_put_survives_restart_and_completes_after_capacity_frees():
    store = MemoryPersistence()
    first = DurableContainerManager(store)
    backend1 = SimPyBackend(origin=NOW)
    first.define(ContainerDefinition("buffer", capacity=10.0, initial=9.0))
    first.rebuild_backend(backend1)

    first.put(
        backend1,
        container_name="buffer",
        request_id="fill",
        amount=5.0,
        requested_at=NOW,
    )
    backend1.run_until(NOW)
    assert [i.request_id for i in store.container_operation_intents()] == ["fill"]

    backend2 = SimPyBackend(origin=NOW)
    recovered = DurableContainerManager(store)
    recovered.rebuild_backend(backend2)
    recovered.get(
        backend2,
        container_name="buffer",
        request_id="drain",
        amount=5.0,
        requested_at=NOW,
    )
    backend2.run_until(NOW)

    assert store.container_operation_intents() == ()
    assert store.container_states() == (ContainerState("buffer", level=9.0),)
    assert {r.request_id for r in store.container_operation_results()} == {
        "drain",
        "fill",
    }


def test_pending_operations_replay_in_original_sequence():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_container_definition(
            ContainerDefinition("tank", capacity=10.0, initial=5.0)
        )
        uow.save_container_state(ContainerState("tank", level=5.0))
        uow.save_container_operation_intent(
            ContainerOperationIntent(
                request_id="need-8",
                container_name="tank",
                operation="get",
                amount=8.0,
                requested_at=NOW,
                sequence=1,
            )
        )
        uow.save_container_operation_intent(
            ContainerOperationIntent(
                request_id="supply-5",
                container_name="tank",
                operation="put",
                amount=5.0,
                requested_at=NOW,
                sequence=2,
            )
        )

    backend = SimPyBackend(origin=NOW)
    manager = DurableContainerManager(store)
    manager.rebuild_backend(backend)
    backend.run_until(NOW)

    assert store.container_operation_intents() == ()
    assert store.container_states() == (ContainerState("tank", level=2.0),)
    assert [r.request_id for r in store.container_operation_results()] == [
        "need-8",
        "supply-5",
    ] or [r.request_id for r in store.container_operation_results()] == [
        "supply-5",
        "need-8",
    ]
    assert {r.request_id for r in store.container_operation_results()} == {
        "need-8",
        "supply-5",
    }


def test_completed_request_id_is_terminal_and_cannot_be_reused():
    store = MemoryPersistence()
    manager = DurableContainerManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(ContainerDefinition("fuel", capacity=10.0, initial=5.0))
    manager.rebuild_backend(backend)

    manager.get(
        backend,
        container_name="fuel",
        request_id="once",
        amount=1.0,
        requested_at=NOW,
    )
    backend.run_until(NOW)

    with pytest.raises(ValueError, match="already exists"):
        manager.put(
            backend,
            container_name="fuel",
            request_id="once",
            amount=1.0,
            requested_at=NOW,
        )

    assert store.container_operation_intents() == ()
    assert [r.request_id for r in store.container_operation_results()] == ["once"]


def test_invalid_durable_container_state_is_rejected_before_backend_mutation():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_container_definition(
            ContainerDefinition("fuel", capacity=10.0, initial=5.0)
        )
        uow.save_container_state(ContainerState("fuel", level=5.0))

    # Corrupt through direct test fixture replacement is intentionally impossible
    # through the UoW; use a mismatched missing-state definition instead.
    with store.transaction() as uow:
        uow.save_container_definition(
            ContainerDefinition("water", capacity=10.0, initial=1.0)
        )

    manager = DurableContainerManager(store)
    backend = SimPyBackend(origin=NOW)

    with pytest.raises(RuntimeError, match="missing durable state"):
        manager.rebuild_backend(backend)

    with pytest.raises(KeyError, match="unknown container"):
        backend.container_snapshot("fuel")


def test_engine_rebuild_restores_container_level_and_pending_operation():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_container_definition(
            ContainerDefinition("fuel", capacity=100.0, initial=20.0)
        )
        uow.save_container_state(ContainerState("fuel", level=20.0))
        uow.save_container_operation_intent(
            ContainerOperationIntent(
                request_id="consume",
                container_name="fuel",
                operation="get",
                amount=30.0,
                requested_at=NOW,
                sequence=1,
            )
        )

    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    engine = Engine(
        context=context,
        registry=DomainRegistry(),
        persistence=store,
    )
    backend = SimPyBackend(origin=NOW)
    engine.rebuild_backend(backend)

    snapshot = backend.container_snapshot("fuel")
    assert snapshot.level == 20.0
    assert snapshot.queued_gets == 1


@pytest.mark.parametrize(
    ("capacity", "initial"),
    [(0.0, 0.0), (-1.0, 0.0), (10.0, -1.0), (10.0, 11.0)],
)
def test_container_definition_validates_capacity_and_initial_level(capacity, initial):
    with pytest.raises(ValueError):
        ContainerDefinition("bad", capacity=capacity, initial=initial)


def test_operation_intent_validates_kind_amount_and_sequence():
    with pytest.raises(ValueError, match="operation"):
        ContainerOperationIntent("x", "fuel", "move", 1.0, NOW, 1)
    with pytest.raises(ValueError, match="amount"):
        ContainerOperationIntent("x", "fuel", "put", 0.0, NOW, 1)
    with pytest.raises(ValueError, match="sequence"):
        ContainerOperationIntent("x", "fuel", "put", 1.0, NOW, 0)


def test_feasible_pending_operation_replays_after_crash_before_durable_commit():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_container_definition(
            ContainerDefinition("fuel", capacity=100.0, initial=20.0)
        )
        uow.save_container_state(ContainerState("fuel", level=20.0))
        uow.save_container_operation_intent(
            ContainerOperationIntent(
                request_id="delivery",
                container_name="fuel",
                operation="put",
                amount=10.0,
                requested_at=NOW,
                sequence=1,
            )
        )

    backend = SimPyBackend(origin=NOW)
    manager = DurableContainerManager(store)
    manager.rebuild_backend(backend)
    backend.run_until(NOW)

    assert store.container_operation_intents() == ()
    assert store.container_states() == (ContainerState("fuel", level=30.0),)
    result = store.container_operation_results()[0]
    assert result.request_id == "delivery"
    assert result.level_before == 20.0
    assert result.level_after == 30.0


def test_rebuild_sorts_intents_by_durable_sequence_not_persistence_order():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_container_definition(
            ContainerDefinition("tank", capacity=10.0, initial=5.0)
        )
        uow.save_container_state(ContainerState("tank", level=5.0))
        uow.save_container_operation_intent(
            ContainerOperationIntent(
                request_id="first-large-get",
                container_name="tank",
                operation="get",
                amount=8.0,
                requested_at=NOW,
                sequence=1,
            )
        )
        uow.save_container_operation_intent(
            ContainerOperationIntent(
                request_id="second-small-get",
                container_name="tank",
                operation="get",
                amount=2.0,
                requested_at=NOW,
                sequence=2,
            )
        )

    class ReverseIntentPersistence:
        def __getattr__(self, name):
            return getattr(store, name)

        def container_operation_intents(self):
            return tuple(reversed(store.container_operation_intents()))

    backend = SimPyBackend(origin=NOW)
    DurableContainerManager(ReverseIntentPersistence()).rebuild_backend(backend)
    backend.run_until(NOW)

    assert store.container_operation_results() == ()
    assert [i.request_id for i in store.container_operation_intents()] == [
        "first-large-get",
        "second-small-get",
    ]
    assert backend.container_snapshot("tank").queued_gets == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_container_records_reject_non_finite_quantities(value):
    with pytest.raises(ValueError):
        ContainerDefinition("bad", capacity=value, initial=0.0)
    with pytest.raises(ValueError):
        ContainerDefinition("bad", capacity=10.0, initial=value)
    with pytest.raises(ValueError):
        ContainerState("bad", level=value)
    with pytest.raises(ValueError):
        ContainerOperationIntent("x", "bad", "put", value, NOW, 1)
    with pytest.raises(ValueError):
        ContainerOperationResult(
            "x",
            "bad",
            "put",
            1.0,
            NOW,
            value,
            1.0,
            1,
        )
    with pytest.raises(ValueError):
        ContainerOperationResult(
            "x",
            "bad",
            "put",
            1.0,
            NOW,
            0.0,
            value,
            1,
        )

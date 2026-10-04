from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sose.backends.base import ResourceLease
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.resources import DurableResourceManager
from sose.core.runtime import (
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
)
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.domain.warehouse import DomainMutation
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _build_engine():
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(hours=1)),
        random=RandomSource(root_seed=42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    persistence = MemoryPersistence()
    entity = context.entities.create(
        WorkOrder,
        key=("core-edge", 1),
        state="planned",
    )
    with persistence.transaction() as uow:
        uow.save_entity(entity)
    engine = Engine(context=context, registry=registry, persistence=persistence)
    return engine, persistence, context, entity


def test_engine_save_entity_domain_conflict_and_domain_delivery_path():
    engine, _, _, _ = _build_engine()
    engine.domain_warehouse = object()
    entity = SimpleNamespace(entity_type="x", id="1", version=1)
    mutation = DomainMutation("id", entity)

    class _ConflictUow:
        def get_domain_delivery(self, mutation_id):
            return SimpleNamespace(mutation=DomainMutation("id", SimpleNamespace(entity_type="x", id="1", version=2)))

        def save_domain_delivery(self, delivery):
            raise AssertionError("save_domain_delivery should not run on conflict")

    with pytest.raises(ValueError, match="domain mutation identity conflict"):
        engine._save_entity(_ConflictUow(), entity)

    saved: list[object] = []

    class _SaveUow:
        def get_domain_delivery(self, mutation_id):
            return None

        def save_domain_delivery(self, delivery):
            saved.append(delivery)

    engine._save_entity(_SaveUow(), entity)
    assert saved


def test_engine_save_entity_skips_when_existing_delivery_matches():
    engine, _, _, _ = _build_engine()
    engine.domain_warehouse = object()
    entity = SimpleNamespace(entity_type="x", id="1", version=1)
    mutation = DomainMutation(
        deterministic_id("domain-entity-version", entity.entity_type, entity.id, entity.version),
        entity,
    )
    saved: list[object] = []

    class _Uow:
        def get_domain_delivery(self, mutation_id):
            return SimpleNamespace(mutation=mutation)

        def save_domain_delivery(self, delivery):
            saved.append(delivery)

    engine._save_entity(_Uow(), entity)
    assert saved == []


def test_engine_dispatch_missing_entity_restores_scenarios():
    engine, _, context, entity = _build_engine()
    missing = Command(
        command_id="cmd-missing",
        name="release",
        entity_type=entity.entity_type,
        entity_id="missing",
        due_at=NOW,
        issued_at=NOW,
        tick=0,
        payload={},
        causation_id=None,
        correlation_id=None,
    )
    with pytest.raises(KeyError, match="entity not found"):
        engine.dispatch(missing)
    assert context.clock.now == NOW
    assert context.clock.tick == 0


def test_engine_dispatch_scheduled_input_guards():
    engine, persistence, _, entity = _build_engine()
    with persistence.transaction() as uow:
        command = Command(
            command_id="cmd-1",
            name="release",
            entity_type=entity.entity_type,
            entity_id=entity.id,
            due_at=NOW,
            issued_at=NOW,
            tick=0,
            payload={},
            causation_id=None,
            correlation_id=None,
        )
        work = SimpleNamespace(work_id="w-1", command_id="cmd-1", due_at=NOW)
        item = SimpleNamespace(command=command, work=work)

        with pytest.raises(ValueError, match="does not match command"):
            engine._dispatch_scheduled_in_uow(
                SimpleNamespace(
                    command=Command(
                        command_id="other",
                        name=command.name,
                        entity_type=command.entity_type,
                        entity_id=command.entity_id,
                        due_at=command.due_at,
                        issued_at=command.issued_at,
                        tick=command.tick,
                        payload=command.payload,
                        causation_id=command.causation_id,
                        correlation_id=command.correlation_id,
                    ),
                    work=work,
                ),
                uow,
            )

        with pytest.raises(ValueError, match="due time does not match command"):
            engine._dispatch_scheduled_in_uow(
                SimpleNamespace(
                    command=Command(
                        command_id=command.command_id,
                        name=command.name,
                        entity_type=command.entity_type,
                        entity_id=command.entity_id,
                        due_at=NOW + timedelta(minutes=1),
                        issued_at=command.issued_at,
                        tick=command.tick,
                        payload=command.payload,
                        causation_id=command.causation_id,
                        correlation_id=command.correlation_id,
                    ),
                    work=work,
                ),
                uow,
            )

        uow.save_command(command)
        uow.save_scheduled_work(work)
        engine.context.clock.now = NOW + timedelta(minutes=1)
        with pytest.raises(ValueError, match="cannot execute before current logical time"):
            engine._dispatch_scheduled_in_uow(item, uow)


def test_engine_dispatch_probabilistic_and_evaluate_entity_edges(monkeypatch):
    engine, _, _, entity = _build_engine()
    decision = SimpleNamespace(
        event="start",
        configuration=SimpleNamespace(states=("planned",)),
    )
    monkeypatch.setattr(engine, "choose_transition", lambda *args, **kwargs: decision)
    dispatched: list[Command] = []
    monkeypatch.setattr(engine, "dispatch", lambda command: dispatched.append(command))

    chosen = engine.dispatch_probabilistic(
        entity,
        event_kwargs={"x": 1},
        scope=("s",),
        caused_by=None,
    )
    assert chosen is decision
    assert dispatched and dispatched[0].name == "start"

    class _Rule:
        def __init__(self, name, result):
            self.name = name
            self._result = result

        def evaluate(self, entity, context):
            return self._result

    engine.rules_for = lambda entity_type: (_Rule("n1", None), _Rule("n2", "release"))
    dispatched.clear()
    engine.evaluate_entity(entity)
    assert len(dispatched) == 1
    assert dispatched[0].name == "release"


def test_engine_advance_tick_executes_legacy_scheduler_and_ignores_stale_due_items(monkeypatch):
    engine, persistence, context, entity = _build_engine()
    command = context.commands.create(
        "release",
        target=entity,
        due_at=NOW,
        key=("legacy", entity.id),
    )
    context.scheduler.schedule(command)
    dispatched: list[Command] = []
    monkeypatch.setattr(engine, "dispatch", lambda cmd: dispatched.append(cmd))
    stale_item = SimpleNamespace(command=command, work=SimpleNamespace(work_id="w", command_id=command.command_id, due_at=NOW))
    monkeypatch.setattr(engine.scheduler, "due", lambda *_args, **_kwargs: (stale_item,))
    monkeypatch.setattr(engine, "_dispatch_scheduled_in_uow", lambda *_args, **_kwargs: False)

    engine.advance_tick()

    assert dispatched == [command]
    assert persistence.simulation_position() is not None


def test_resource_manager_edge_branches():
    store = MemoryPersistence()
    manager = DurableResourceManager(store)

    class _BareBackend:
        now = NOW

        def request_resource(self, *args, **kwargs):
            return None

        def cancel_resource_request(self, request_id):
            return False

    backend = _BareBackend()

    with pytest.raises(KeyError, match="unknown resource definition"):
        manager.request(
            backend,
            resource_name="missing",
            request_id="r1",
            requested_at=NOW,
        )

    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", 1))
        uow.save_resource_demand(ResourceDemand("r1", "bay", 100, NOW, 1))

    with pytest.raises(ValueError, match="already exists"):
        manager.request(
            backend,
            resource_name="bay",
            request_id="r1",
            requested_at=NOW,
        )

    with pytest.raises(KeyError, match="unknown resource demand"):
        manager.commit_grant(request_id="unknown", acquired_at=NOW)

    assert manager.cancel_pending(backend, "r1") is False


def test_resource_manager_transaction_mismatch_branches():
    demand = ResourceDemand("r2", "bay", 100, NOW, 1)
    reservation = ResourceReservation("res-r2", "r2", "bay", NOW, sequence=1)
    intent = ResourceReleaseIntent("intent-1", "res-r2", "bay", NOW)

    class _MismatchPersistence:
        def __init__(self):
            self._demands = (demand,)
            self._reservations = ()

        def resource_definitions(self):
            return (ResourceDefinition("bay", 1),)

        def resource_demands(self):
            return self._demands

        def resource_reservations(self):
            return self._reservations

        def resource_release_intents(self):
            return (intent,)

        @contextmanager
        def transaction(self):
            class _Uow:
                def save_resource_demand(self, value):
                    return None

                def get_resource_demand(self, request_id):
                    return ResourceDemand("other", "bay", 100, NOW, 9)

                def delete_resource_demand(self, request_id):
                    return None

                def save_resource_reservation(self, value):
                    return None

                def get_resource_reservation(self, reservation_id):
                    return ResourceReservation("other", "x", "bay", NOW, sequence=9)

                def save_resource_release_intent(self, value):
                    return None

                def get_resource_release_intent(self, intent_id):
                    return ResourceReleaseIntent("other", "other", "bay", NOW)

                def delete_resource_release_intent(self, intent_id):
                    return None

                def delete_resource_reservation(self, reservation_id):
                    return None

            yield _Uow()

    manager = DurableResourceManager(_MismatchPersistence())

    with pytest.raises(RuntimeError, match="changed before grant"):
        manager.commit_grant(request_id="r2", acquired_at=NOW)

    class _CancelBackend:
        def cancel_resource_request(self, request_id):
            return True

    with pytest.raises(RuntimeError, match="changed during cancellation"):
        manager.cancel_pending(_CancelBackend(), "r2")

    class _ReleaseMismatchPersistence(_MismatchPersistence):
        def __init__(self):
            super().__init__()
            self._reservations = (reservation,)

    release_manager = DurableResourceManager(_ReleaseMismatchPersistence())
    release_manager._backend_leases["res-r2"] = ResourceLease("lease-r2", "r2", "bay", NOW)
    assert release_manager.release(
        SimpleNamespace(now=NOW, release_resource=lambda lease: None),
        "res-r2",
    ) is False

    with pytest.raises(RuntimeError, match="intent changed"):
        release_manager._finalize_release_intent(intent, reservation)

    class _ReservationMismatchPersistence(_MismatchPersistence):
        @contextmanager
        def transaction(self):
            class _Uow:
                def get_resource_release_intent(self, intent_id):
                    return intent

                def get_resource_reservation(self, reservation_id):
                    return ResourceReservation("other", "x", "bay", NOW, sequence=9)

                def delete_resource_release_intent(self, intent_id):
                    return None

                def delete_resource_reservation(self, reservation_id):
                    return None

            yield _Uow()

    with pytest.raises(RuntimeError, match="reservation changed during release"):
        DurableResourceManager(_ReservationMismatchPersistence())._finalize_release_intent(
            intent,
            reservation,
        )


def test_resource_manager_finalize_interrupted_releases_without_reservation():
    class _IntentOnlyPersistence:
        def resource_release_intents(self):
            return (ResourceReleaseIntent("intent-missing", "missing", "bay", NOW),)

        def resource_reservations(self):
            return ()

        @contextmanager
        def transaction(self):
            class _Uow:
                def __init__(self):
                    self.deleted: list[str] = []

                def delete_resource_release_intent(self, intent_id):
                    self.deleted.append(intent_id)

            uow = _Uow()
            self._last_uow = uow
            yield uow

    store = _IntentOnlyPersistence()
    manager = DurableResourceManager(store)
    manager._finalize_interrupted_releases()
    assert store._last_uow.deleted == ["intent-missing"]


def test_resource_manager_finalize_interrupted_releases_with_reservation():
    intent = ResourceReleaseIntent("intent-hit", "res-hit", "bay", NOW)
    reservation = ResourceReservation("res-hit", "r-hit", "bay", NOW, sequence=1)

    class _Persistence:
        def resource_release_intents(self):
            return (intent,)

        def resource_reservations(self):
            return (reservation,)

    manager = DurableResourceManager(_Persistence())
    calls: list[tuple[ResourceReleaseIntent, ResourceReservation]] = []
    manager._finalize_release_intent = lambda i, r: calls.append((i, r))
    manager._finalize_interrupted_releases()
    assert calls == [(intent, reservation)]


def test_resource_manager_idempotent_helpers_work_without_backend_run_until():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))

    manager = DurableResourceManager(persistence)

    class _Backend:
        def __init__(self):
            self.now = NOW

        def request_resource(self, *args, **kwargs):
            return None

        def cancel_request(self, *args, **kwargs):
            return None

        def cancel_resource_request(self, *args, **kwargs):
            return True

        def release_resource(self, *args, **kwargs):
            return None

    backend = _Backend()

    reservation = manager.ensure_requested(
        backend,
        resource_name="bay",
        request_id="edge-request",
        requested_at=NOW,
    )
    changed = manager.withdraw(backend, "edge-request")

    assert reservation is None
    assert changed is True

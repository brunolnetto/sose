from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.backends.base import ResourceLease, ResourcePreemption
from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.preemption import DurablePreemptiveResourceManager
from sose.core.randomness import RandomSource
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
)
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry
from sose.persistence.memory import MemoryPersistence

NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def test_preemption_atomically_replaces_holder_and_persists_terminal_result():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    manager.rebuild_backend(backend)

    manager.request(
        backend,
        resource_name="crew",
        request_id="planned",
        requested_at=NOW,
        priority=100,
    )
    backend.run_until(NOW)

    manager.request(
        backend,
        resource_name="crew",
        request_id="emergency",
        requested_at=NOW,
        priority=1,
        preempt=True,
    )
    backend.run_until(NOW)

    reservations = store.preemptive_resource_reservations()
    assert [r.request_id for r in reservations] == ["emergency"]
    assert store.preemptive_resource_demands() == ()

    results = store.resource_preemption_results()
    assert len(results) == 1
    assert results[0].displaced_request_id == "planned"
    assert results[0].preempting_request_id == "emergency"
    assert results[0].resource_name == "crew"
    assert results[0].preempted_at == NOW


class ControlledPreemptiveBackend:
    def __init__(self):
        self.now = NOW
        self.capacity = {}
        self.requests = {}
        self.active = {}

    def create_preemptive_resource(self, name, *, capacity=1):
        self.capacity[name] = capacity
        self.active[name] = {}

    def request_preemptive_resource(
        self,
        name,
        *,
        request_id,
        on_acquired,
        on_preempted,
        priority=100,
        preempt=True,
    ):
        self.requests[request_id] = (name, on_acquired, on_preempted, priority, preempt)

    def acquire(self, request_id):
        name, on_acquired, _, _, _ = self.requests[request_id]
        lease = ResourceLease(
            lease_id=f"lease-{request_id}",
            request_id=request_id,
            resource_name=name,
            acquired_at=self.now,
        )
        self.active[name][request_id] = lease
        on_acquired(lease)

    def preempt(self, displaced_request_id, *, by):
        name, _, on_preempted, _, _ = self.requests[displaced_request_id]
        lease = self.active[name].pop(displaced_request_id)
        on_preempted(
            ResourcePreemption(
                lease_id=lease.lease_id,
                request_id=displaced_request_id,
                resource_name=name,
                preempted_at=self.now,
                preempted_by=by,
            )
        )

    def release_preemptive_resource(self, lease):
        lease_id = lease.lease_id if hasattr(lease, "lease_id") else lease
        for active in self.active.values():
            for request_id, current in list(active.items()):
                if current.lease_id == lease_id:
                    active.pop(request_id)
                    return
        raise KeyError(lease_id)

    def preemptive_resource_snapshot(self, name):
        raise NotImplementedError


def _seed_holder(store, backend):
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    manager.rebuild_backend(backend)
    manager.request(
        backend,
        resource_name="crew",
        request_id="holder",
        requested_at=NOW,
        priority=100,
    )
    backend.acquire("holder")
    return manager


def test_acquisition_before_preemption_signal_does_not_create_double_reservation():
    store = MemoryPersistence()
    backend = ControlledPreemptiveBackend()
    manager = _seed_holder(store, backend)

    manager.request(
        backend,
        resource_name="crew",
        request_id="urgent",
        requested_at=NOW,
        priority=1,
        preempt=True,
    )
    backend.acquire("urgent")

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["holder"]
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["urgent"]

    backend.preempt("holder", by="urgent")

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["urgent"]
    assert store.preemptive_resource_demands() == ()
    assert [r.preempting_request_id for r in store.resource_preemption_results()] == ["urgent"]


def test_preemption_signal_before_acquisition_keeps_old_truth_until_pair_is_complete():
    store = MemoryPersistence()
    backend = ControlledPreemptiveBackend()
    manager = _seed_holder(store, backend)

    manager.request(
        backend,
        resource_name="crew",
        request_id="urgent",
        requested_at=NOW,
        priority=1,
        preempt=True,
    )
    backend.preempt("holder", by="urgent")

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["holder"]
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["urgent"]

    backend.acquire("urgent")

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["urgent"]
    assert store.preemptive_resource_demands() == ()


def test_crash_between_preemption_callbacks_replays_from_last_durable_truth():
    store = MemoryPersistence()
    backend1 = ControlledPreemptiveBackend()
    first = _seed_holder(store, backend1)

    first.request(
        backend1,
        resource_name="crew",
        request_id="urgent",
        requested_at=NOW,
        priority=1,
        preempt=True,
    )
    backend1.acquire("urgent")

    # Crash here: ephemeral backend thinks urgent acquired, but durable truth is unchanged.
    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["holder"]
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["urgent"]

    backend2 = ControlledPreemptiveBackend()
    recovered = DurablePreemptiveResourceManager(store)
    recovered.rebuild_backend(backend2)
    backend2.acquire("holder")
    backend2.acquire("urgent")
    backend2.preempt("holder", by="urgent")

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["urgent"]
    assert store.preemptive_resource_demands() == ()
    assert len(store.resource_preemption_results()) == 1


def test_nonpreempting_waiter_remains_demand_until_holder_is_released():
    store = MemoryPersistence()
    backend = ControlledPreemptiveBackend()
    manager = _seed_holder(store, backend)

    manager.request(
        backend,
        resource_name="crew",
        request_id="waiter",
        requested_at=NOW,
        priority=1,
        preempt=False,
    )

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["holder"]
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["waiter"]


def test_release_intent_prevents_synchronous_waiter_grant_from_double_allocating():
    store = MemoryPersistence()
    backend = ControlledPreemptiveBackend()
    manager = _seed_holder(store, backend)

    manager.request(
        backend,
        resource_name="crew",
        request_id="waiter",
        requested_at=NOW,
        priority=100,
        preempt=False,
    )

    holder = store.preemptive_resource_reservations()[0]

    class GrantOnReleaseBackend(ControlledPreemptiveBackend):
        pass

    # Simulate backend granting waiter during release before durable finalization.
    original_release = backend.release_preemptive_resource

    def release_and_grant(lease):
        original_release(lease)
        backend.acquire("waiter")

    backend.release_preemptive_resource = release_and_grant

    assert manager.release(backend, holder.reservation_id) is True

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["waiter"]
    assert store.preemptive_resource_demands() == ()
    assert store.preemptive_resource_release_intents() == ()


def test_rebuild_finalizes_interrupted_release_before_replaying_waiter():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_preemptive_resource_definition(
            PreemptiveResourceDefinition("crew", capacity=1)
        )
        uow.save_preemptive_resource_reservation(
            PreemptiveResourceReservation(
                reservation_id="res-holder",
                request_id="holder",
                resource_name="crew",
                acquired_at=NOW,
                priority=100,
                sequence=1,
            )
        )
        uow.save_preemptive_resource_demand(
            PreemptiveResourceDemand(
                request_id="waiter",
                resource_name="crew",
                priority=100,
                preempt=False,
                requested_at=NOW,
                sequence=2,
            )
        )
        uow.save_preemptive_resource_release_intent(
            PreemptiveResourceReleaseIntent(
                intent_id="release-holder",
                reservation_id="res-holder",
                resource_name="crew",
                requested_at=NOW,
            )
        )

    backend = ControlledPreemptiveBackend()
    manager = DurablePreemptiveResourceManager(store)
    manager.rebuild_backend(backend)

    assert store.preemptive_resource_release_intents() == ()
    assert store.preemptive_resource_reservations() == ()
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["waiter"]
    assert "holder" not in backend.requests
    assert "waiter" in backend.requests


def test_engine_rebuild_restores_preemptive_resource_state():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_preemptive_resource_definition(
            PreemptiveResourceDefinition("crew", capacity=1)
        )
        uow.save_preemptive_resource_reservation(
            PreemptiveResourceReservation(
                reservation_id="res-holder",
                request_id="holder",
                resource_name="crew",
                acquired_at=NOW,
                priority=100,
                sequence=1,
            )
        )

    context = SimulationContext(
        clock=SimulationClock(NOW, timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    engine = Engine(context=context, registry=DomainRegistry(), persistence=store)
    backend = ControlledPreemptiveBackend()

    engine.rebuild_backend(backend)

    assert "holder" in backend.requests


def test_models_validate_preemptive_resource_invariants():
    with pytest.raises(ValueError, match="capacity"):
        PreemptiveResourceDefinition("crew", capacity=0)
    with pytest.raises(ValueError, match="sequence"):
        PreemptiveResourceDemand("x", "crew", 1, True, NOW, 0)
    with pytest.raises(ValueError, match="sequence"):
        PreemptiveResourceReservation("r", "x", "crew", NOW, 1, 0)

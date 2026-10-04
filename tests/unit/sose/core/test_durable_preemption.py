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
    ResourcePreemptionResult,
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


def test_rebuild_clears_stale_handshake_state_from_dead_backend():
    store = MemoryPersistence()
    backend1 = ControlledPreemptiveBackend()
    manager = _seed_holder(store, backend1)

    manager.request(
        backend1,
        resource_name="crew",
        request_id="urgent",
        requested_at=NOW,
        priority=1,
        preempt=True,
    )
    backend1.acquire("urgent")

    assert "urgent" in manager._pending_grants

    backend2 = ControlledPreemptiveBackend()
    manager.rebuild_backend(backend2)

    assert manager._pending_grants == {}
    assert manager._pending_preemptions == {}
    assert manager._backend_leases == {}
    assert manager._pending_callbacks == {}

    backend2.acquire("holder")
    backend2.preempt("holder", by="urgent")

    # Fresh preemption callback alone must not pair with the stale lease.
    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["holder"]
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["urgent"]

    backend2.acquire("urgent")

    assert [r.request_id for r in store.preemptive_resource_reservations()] == ["urgent"]


def test_rebuild_does_not_run_unrelated_current_time_backend_events():
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

    backend = SimPyBackend(origin=NOW)
    observed = []
    backend.schedule_at(NOW, lambda: observed.append("unrelated"), priority=1)

    manager = DurablePreemptiveResourceManager(store)
    manager.rebuild_backend(backend)

    assert observed == []

    backend.run_until(NOW)
    assert observed == ["unrelated"]



def test_cancel_pending_before_preemptive_lifecycle_registration():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    manager.rebuild_backend(backend)

    manager.request(
        backend,
        resource_name="crew",
        request_id="waiter",
        requested_at=NOW,
        priority=100,
        preempt=False,
    )

    assert [d.request_id for d in store.preemptive_resource_demands()] == ["waiter"]
    assert manager.cancel_pending(backend, "waiter") is True
    assert store.preemptive_resource_demands() == ()

    backend.run_until(NOW)

    assert store.preemptive_resource_reservations() == ()
    assert backend.preemptive_resource_snapshot("crew").in_use == 0


def test_validate_rebuild_rejects_overlap_and_unknown_result_definition():
    class _OverlapPersistence:
        def preemptive_resource_definitions(self):
            return (PreemptiveResourceDefinition("crew", capacity=1),)

        def preemptive_resource_reservations(self):
            return (
                PreemptiveResourceReservation(
                    reservation_id="res-shared",
                    request_id="shared",
                    resource_name="crew",
                    acquired_at=NOW,
                    priority=100,
                    sequence=1,
                ),
            )

        def preemptive_resource_demands(self):
            return (
                PreemptiveResourceDemand(
                    request_id="shared",
                    resource_name="crew",
                    priority=100,
                    preempt=True,
                    requested_at=NOW,
                    sequence=1,
                ),
            )

        def preemptive_resource_release_intents(self):
            return ()

        def resource_preemption_results(self):
            return ()

    manager = DurablePreemptiveResourceManager(_OverlapPersistence())
    with pytest.raises(RuntimeError, match="both pending and reserved"):
        manager.validate_rebuild()

    class _UnknownResultDefinitionPersistence:
        def preemptive_resource_definitions(self):
            return (PreemptiveResourceDefinition("crew", capacity=1),)

        def preemptive_resource_reservations(self):
            return ()

        def preemptive_resource_demands(self):
            return ()

        def preemptive_resource_release_intents(self):
            return ()

        def resource_preemption_results(self):
            return (
                ResourcePreemptionResult(
                    result_id="preempt-1",
                    resource_name="ghost",
                    displaced_reservation_id="res-holder",
                    displaced_request_id="holder",
                    preempting_request_id="urgent",
                    successor_reservation_id="res-urgent",
                    preempted_at=NOW,
                    sequence=1,
                ),
            )

    manager = DurablePreemptiveResourceManager(_UnknownResultDefinitionPersistence())
    with pytest.raises(RuntimeError, match="unknown definition"):
        manager.validate_rebuild()


def test_try_commit_preemption_handles_incomplete_and_cross_resource_pairs():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager._pending_grants["urgent"] = ResourceLease(
        lease_id="lease-urgent",
        request_id="urgent",
        resource_name="crew",
        acquired_at=NOW,
    )
    assert manager._try_commit_preemption("urgent") is False

    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    with store.transaction() as uow:
        uow.save_preemptive_resource_definition(
            PreemptiveResourceDefinition("crew", capacity=1)
        )
        uow.save_preemptive_resource_definition(
            PreemptiveResourceDefinition("other", capacity=1)
        )
        uow.save_preemptive_resource_demand(
            PreemptiveResourceDemand(
                request_id="urgent",
                resource_name="other",
                priority=1,
                preempt=True,
                requested_at=NOW,
                sequence=2,
            )
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
    manager._pending_grants["urgent"] = ResourceLease(
        lease_id="lease-urgent",
        request_id="urgent",
        resource_name="other",
        acquired_at=NOW,
    )
    manager._pending_preemptions["urgent"] = ResourcePreemption(
        lease_id="lease-holder",
        request_id="holder",
        resource_name="crew",
        preempted_at=NOW,
        preempted_by="urgent",
    )
    with pytest.raises(RuntimeError, match="crosses resources"):
        manager._try_commit_preemption("urgent")


def test_try_commit_normal_grant_returns_false_while_release_or_capacity_blocks():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
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
    manager._pending_grants["waiter"] = ResourceLease(
        lease_id="lease-waiter",
        request_id="waiter",
        resource_name="crew",
        acquired_at=NOW,
    )
    assert manager._try_commit_normal_grant("waiter") is False


def test_validate_rebuild_rejects_unknown_references_and_capacity_overflow():
    class _UnknownReservationPersistence:
        def preemptive_resource_definitions(self):
            return ()

        def preemptive_resource_reservations(self):
            return (
                PreemptiveResourceReservation(
                    reservation_id="res-1",
                    request_id="req-1",
                    resource_name="ghost",
                    acquired_at=NOW,
                    priority=100,
                    sequence=1,
                ),
            )

        def preemptive_resource_demands(self):
            return ()

        def preemptive_resource_release_intents(self):
            return ()

        def resource_preemption_results(self):
            return ()

    with pytest.raises(RuntimeError, match="reservation references unknown definition"):
        DurablePreemptiveResourceManager(_UnknownReservationPersistence()).validate_rebuild()

    class _UnknownDemandPersistence:
        def preemptive_resource_definitions(self):
            return ()

        def preemptive_resource_reservations(self):
            return ()

        def preemptive_resource_demands(self):
            return (
                PreemptiveResourceDemand(
                    request_id="req-1",
                    resource_name="ghost",
                    priority=1,
                    preempt=True,
                    requested_at=NOW,
                    sequence=1,
                ),
            )

        def preemptive_resource_release_intents(self):
            return ()

        def resource_preemption_results(self):
            return ()

    with pytest.raises(RuntimeError, match="demand references unknown definition"):
        DurablePreemptiveResourceManager(_UnknownDemandPersistence()).validate_rebuild()

    class _BadReleasePersistence:
        def preemptive_resource_definitions(self):
            return (PreemptiveResourceDefinition("crew", capacity=1),)

        def preemptive_resource_reservations(self):
            return (
                PreemptiveResourceReservation(
                    reservation_id="res-1",
                    request_id="req-1",
                    resource_name="crew",
                    acquired_at=NOW,
                    priority=100,
                    sequence=1,
                ),
            )

        def preemptive_resource_demands(self):
            return ()

        def preemptive_resource_release_intents(self):
            return (
                PreemptiveResourceReleaseIntent(
                    intent_id="intent-1",
                    reservation_id="res-1",
                    resource_name="other",
                    requested_at=NOW,
                ),
            )

        def resource_preemption_results(self):
            return ()

    with pytest.raises(RuntimeError, match="targets wrong resource"):
        DurablePreemptiveResourceManager(_BadReleasePersistence()).validate_rebuild()

    class _OverCapacityPersistence:
        def preemptive_resource_definitions(self):
            return (PreemptiveResourceDefinition("crew", capacity=1),)

        def preemptive_resource_reservations(self):
            return (
                PreemptiveResourceReservation(
                    reservation_id="res-1",
                    request_id="req-1",
                    resource_name="crew",
                    acquired_at=NOW,
                    priority=100,
                    sequence=1,
                ),
                PreemptiveResourceReservation(
                    reservation_id="res-2",
                    request_id="req-2",
                    resource_name="crew",
                    acquired_at=NOW,
                    priority=100,
                    sequence=2,
                ),
            )

        def preemptive_resource_demands(self):
            return ()

        def preemptive_resource_release_intents(self):
            return ()

        def resource_preemption_results(self):
            return ()

    with pytest.raises(RuntimeError, match="exceeds durable capacity"):
        DurablePreemptiveResourceManager(_OverCapacityPersistence()).validate_rebuild()


def test_ensure_requested_registers_pending_callback_and_runs_backend_clock():
    class _RunUntilBackend:
        def __init__(self):
            self.now = NOW
            self.calls = []

        def run_until(self, at):
            self.calls.append(at)

    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    with store.transaction() as uow:
        uow.save_preemptive_resource_demand(
            PreemptiveResourceDemand(
                request_id="pending",
                resource_name="crew",
                priority=5,
                preempt=True,
                requested_at=NOW,
                sequence=1,
            )
        )

    backend = _RunUntilBackend()
    received = []
    assert manager.ensure_requested(
        backend,
        resource_name="crew",
        request_id="pending",
        requested_at=NOW,
        on_acquired=received.append,
    ) is None
    assert "pending" in manager._pending_callbacks
    assert backend.calls == [NOW]


def test_cancel_pending_handles_backend_refusal_and_demand_race():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    with store.transaction() as uow:
        uow.save_preemptive_resource_demand(
            PreemptiveResourceDemand(
                request_id="pending",
                resource_name="crew",
                priority=5,
                preempt=True,
                requested_at=NOW,
                sequence=1,
            )
        )

    class _RefusingBackend:
        def cancel_preemptive_resource_request(self, request_id):
            return False

    assert manager.cancel_pending(_RefusingBackend(), "pending") is False

    demand = PreemptiveResourceDemand(
        request_id="pending",
        resource_name="crew",
        priority=5,
        preempt=True,
        requested_at=NOW,
        sequence=1,
    )

    class _RaceUow:
        def get_preemptive_resource_demand(self, request_id):
            return None

        def delete_preemptive_resource_demand(self, request_id):
            raise AssertionError("race path should fail before mutation")

    class _RaceTransaction:
        def __enter__(self):
            return _RaceUow()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _RacePersistence:
        def preemptive_resource_demands(self):
            return (demand,)

        def preemptive_resource_reservations(self):
            return ()

        def transaction(self):
            return _RaceTransaction()

    class _ApprovingBackend:
        def cancel_preemptive_resource_request(self, request_id):
            return True

    manager = DurablePreemptiveResourceManager(_RacePersistence())
    with pytest.raises(RuntimeError, match="changed during cancellation"):
        manager.cancel_pending(_ApprovingBackend(), "pending")


def test_release_and_preemption_callback_guardrails():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    with store.transaction() as uow:
        uow.save_preemptive_resource_reservation(
            PreemptiveResourceReservation(
                reservation_id="res-1",
                request_id="holder",
                resource_name="crew",
                acquired_at=NOW,
                priority=100,
                sequence=1,
            )
        )

    class _Backend:
        now = NOW

        def release_preemptive_resource(self, lease):
            raise AssertionError("should not release without reconstructed lease")

    with pytest.raises(RuntimeError, match="lease is not reconstructed"):
        manager.release(_Backend(), "res-1")
    assert manager.release(_Backend(), "missing") is False

    with pytest.raises(RuntimeError, match="missing preempting request identity"):
        manager._record_preempted(
            ResourcePreemption(
                lease_id="lease-holder",
                request_id="holder",
                resource_name="crew",
                preempted_at=NOW,
                preempted_by="",
            )
        )

    with pytest.raises(RuntimeError, match="targets wrong resource"):
        manager._record_preempted(
            ResourcePreemption(
                lease_id="lease-holder",
                request_id="holder",
                resource_name="other",
                preempted_at=NOW,
                preempted_by="urgent",
            )
        )


def test_finalize_interrupted_releases_cleans_orphaned_intents():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    reservation = PreemptiveResourceReservation(
        reservation_id="res-1",
        request_id="holder",
        resource_name="crew",
        acquired_at=NOW,
        priority=100,
        sequence=1,
    )
    intent = PreemptiveResourceReleaseIntent(
        intent_id="rel-1",
        reservation_id=reservation.reservation_id,
        resource_name="crew",
        requested_at=NOW,
    )
    with store.transaction() as uow:
        uow.save_preemptive_resource_reservation(reservation)
        uow.save_preemptive_resource_release_intent(intent)
        uow.delete_preemptive_resource_reservation(reservation.reservation_id)

    manager._finalize_interrupted_releases()
    assert store.preemptive_resource_release_intents() == ()


def test_validate_rebuild_rejects_release_with_unknown_reservation():
    class _UnknownReleasePersistence:
        def preemptive_resource_definitions(self):
            return (PreemptiveResourceDefinition("crew", capacity=1),)

        def preemptive_resource_reservations(self):
            return ()

        def preemptive_resource_demands(self):
            return ()

        def preemptive_resource_release_intents(self):
            return (
                PreemptiveResourceReleaseIntent(
                    intent_id="rel-1",
                    reservation_id="missing",
                    resource_name="crew",
                    requested_at=NOW,
                ),
            )

        def resource_preemption_results(self):
            return ()

    with pytest.raises(RuntimeError, match="references unknown reservation"):
        DurablePreemptiveResourceManager(_UnknownReleasePersistence()).validate_rebuild()


def test_ensure_requested_new_request_without_run_until_and_duplicate_request_rejected():
    class _NoRunUntilBackend:
        def __init__(self):
            self.submitted = []

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
            self.submitted.append((name, request_id, priority, preempt))

    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    backend = _NoRunUntilBackend()

    assert manager.ensure_requested(
        backend,
        resource_name="crew",
        request_id="new-request",
        requested_at=NOW,
        priority=7,
        preempt=False,
        on_acquired=lambda _: None,
    ) is None
    assert backend.submitted == [("crew", "new-request", 7, False)]
    assert [d.request_id for d in store.preemptive_resource_demands()] == ["new-request"]
    assert "new-request" in manager._pending_callbacks

    with pytest.raises(ValueError, match="already exists"):
        manager.request(
            backend,
            resource_name="crew",
            request_id="new-request",
            requested_at=NOW,
        )


def test_withdraw_and_cancel_pending_guard_paths():
    class _Backend:
        def __init__(self, *, cancel_result=True):
            self.now = NOW
            self.cancel_result = cancel_result
            self.cancel_calls = []
            self.run_calls = []
            self.release_calls = []

        def cancel_preemptive_resource_request(self, request_id):
            self.cancel_calls.append(request_id)
            return self.cancel_result

        def run_until(self, at):
            self.run_calls.append(at)

        def release_preemptive_resource(self, lease):
            self.release_calls.append(lease.lease_id)

    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    assert manager.cancel_pending(_Backend(), "missing") is False

    with store.transaction() as uow:
        uow.save_preemptive_resource_demand(
            PreemptiveResourceDemand(
                request_id="pending",
                resource_name="crew",
                priority=100,
                preempt=False,
                requested_at=NOW,
                sequence=1,
            )
        )
    refusing_backend = _Backend(cancel_result=False)
    assert manager.withdraw(refusing_backend, "pending") is False
    assert refusing_backend.run_calls == [NOW]

    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    reservation = PreemptiveResourceReservation(
        reservation_id="res-1",
        request_id="holder",
        resource_name="crew",
        acquired_at=NOW,
        priority=100,
        sequence=1,
    )
    with store.transaction() as uow:
        uow.save_preemptive_resource_reservation(reservation)
    manager._backend_leases[reservation.reservation_id] = ResourceLease(
        lease_id="lease-holder",
        request_id="holder",
        resource_name="crew",
        acquired_at=NOW,
    )
    backend = _Backend()
    assert manager.withdraw(backend, "holder") is True
    assert backend.release_calls == ["lease-holder"]
    assert backend.run_calls == [NOW]


def test_internal_preemption_guard_branches():
    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager._record_acquired(
        request_id="unknown",
        lease=ResourceLease(
            lease_id="lease-unknown",
            request_id="unknown",
            resource_name="crew",
            acquired_at=NOW,
        ),
    )
    assert manager._pending_grants == {}

    manager._record_preempted(
        ResourcePreemption(
            lease_id="lease-unknown",
            request_id="unknown",
            resource_name="crew",
            preempted_at=NOW,
            preempted_by="urgent",
        )
    )

    manager._pending_grants["urgent"] = ResourceLease(
        lease_id="lease-urgent",
        request_id="urgent",
        resource_name="crew",
        acquired_at=NOW,
    )
    manager._pending_preemptions["urgent"] = ResourcePreemption(
        lease_id="lease-holder",
        request_id="holder",
        resource_name="crew",
        preempted_at=NOW,
        preempted_by="urgent",
    )
    assert manager._try_commit_preemption("urgent") is False
    assert manager._try_commit_normal_grant("urgent") is False


def test_internal_race_branches_for_preemption_commit_and_finalize():
    demand = PreemptiveResourceDemand(
        request_id="urgent",
        resource_name="crew",
        priority=1,
        preempt=True,
        requested_at=NOW,
        sequence=2,
    )
    displaced = PreemptiveResourceReservation(
        reservation_id="res-holder",
        request_id="holder",
        resource_name="crew",
        acquired_at=NOW,
        priority=100,
        sequence=1,
    )
    lease = ResourceLease(
        lease_id="lease-urgent",
        request_id="urgent",
        resource_name="crew",
        acquired_at=NOW,
    )
    event = ResourcePreemption(
        lease_id="lease-holder",
        request_id="holder",
        resource_name="crew",
        preempted_at=NOW,
        preempted_by="urgent",
    )

    class _RaceUow:
        def __init__(self, *, demand_value, reservation_value):
            self._demand_value = demand_value
            self._reservation_value = reservation_value

        def get_preemptive_resource_demand(self, request_id):
            return self._demand_value

        def get_preemptive_resource_reservation(self, reservation_id):
            return self._reservation_value

        def delete_preemptive_resource_demand(self, request_id):
            raise AssertionError("race path should exit before mutation")

        def delete_preemptive_resource_reservation(self, reservation_id):
            raise AssertionError("race path should exit before mutation")

        def save_preemptive_resource_reservation(self, reservation):
            raise AssertionError("race path should exit before mutation")

        def save_resource_preemption_result(self, result):
            raise AssertionError("race path should exit before mutation")

    class _RaceTransaction:
        def __init__(self, uow):
            self._uow = uow

        def __enter__(self):
            return self._uow

        def __exit__(self, exc_type, exc, tb):
            return False

    class _RacePersistence:
        def __init__(self, *, demand_value, reservation_value):
            self._demand_value = demand_value
            self._reservation_value = reservation_value

        def preemptive_resource_demands(self):
            return (demand,)

        def preemptive_resource_reservations(self):
            return (displaced,)

        def transaction(self):
            return _RaceTransaction(
                _RaceUow(
                    demand_value=self._demand_value,
                    reservation_value=self._reservation_value,
                )
            )

    manager = DurablePreemptiveResourceManager(
        _RacePersistence(demand_value=None, reservation_value=displaced)
    )
    manager._pending_grants["urgent"] = lease
    manager._pending_preemptions["urgent"] = event
    assert manager._try_commit_preemption("urgent") is False

    manager = DurablePreemptiveResourceManager(
        _RacePersistence(demand_value=demand, reservation_value=None)
    )
    manager._pending_grants["urgent"] = lease
    manager._pending_preemptions["urgent"] = event
    assert manager._try_commit_preemption("urgent") is False

    store = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(store)
    manager.define(PreemptiveResourceDefinition("crew", capacity=1))
    reservation = PreemptiveResourceReservation(
        reservation_id="res-1",
        request_id="holder",
        resource_name="crew",
        acquired_at=NOW,
        priority=100,
        sequence=1,
    )
    intent = PreemptiveResourceReleaseIntent(
        intent_id="rel-1",
        reservation_id="res-1",
        resource_name="crew",
        requested_at=NOW,
    )
    with store.transaction() as uow:
        uow.save_preemptive_resource_reservation(reservation)
        uow.save_preemptive_resource_release_intent(intent)

    with pytest.raises(RuntimeError, match="intent changed"):
        manager._finalize_release_intent(
            PreemptiveResourceReleaseIntent(
                intent_id="rel-1",
                reservation_id="res-1",
                resource_name="other",
                requested_at=NOW,
            ),
            reservation,
        )
    with pytest.raises(RuntimeError, match="reservation changed"):
        manager._finalize_release_intent(
            intent,
            PreemptiveResourceReservation(
                reservation_id="res-1",
                request_id="holder",
                resource_name="other",
                acquired_at=NOW,
                priority=100,
                sequence=1,
            ),
        )


def test_definition_and_callback_helpers_cover_internal_branches():
    manager = DurablePreemptiveResourceManager(MemoryPersistence())
    with pytest.raises(KeyError, match="unknown preemptive resource definition"):
        manager._definition("missing")

    manager._pending_grants = {
        "match-preempt": ResourceLease("lease-1", "match-preempt", "crew", NOW),
        "match-grant": ResourceLease("lease-2", "match-grant", "crew", NOW),
        "skip-other": ResourceLease("lease-3", "skip-other", "other", NOW),
    }
    calls = []

    def _preemption(request_id):
        calls.append(("preemption", request_id))
        return request_id == "match-preempt"

    def _grant(request_id):
        calls.append(("grant", request_id))
        return False

    manager._try_commit_preemption = _preemption
    manager._try_commit_normal_grant = _grant
    manager._pending_callbacks["notify"] = lambda reservation: calls.append(
        ("callback", reservation.request_id)
    )
    manager._reconcile_pending_grants("crew")
    manager._notify_acquired(
        PreemptiveResourceReservation(
            reservation_id="res-notify",
            request_id="notify",
            resource_name="crew",
            acquired_at=NOW,
            priority=1,
            sequence=1,
        )
    )

    assert ("preemption", "match-preempt") in calls
    assert ("preemption", "match-grant") in calls
    assert ("grant", "match-grant") in calls
    assert ("callback", "notify") in calls

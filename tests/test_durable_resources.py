from datetime import datetime, timezone

from sose.core.resources import (
    DurableResourceManager,
    ResourceDefinition,
    ResourceDemand,
    ResourceReservation,
)
from sose.backends.base import ResourceLease
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def test_resource_state_round_trips_through_persistence():
    store = MemoryPersistence()
    definition = ResourceDefinition("technicians", capacity=2)
    demand = ResourceDemand(
        request_id="wo-1",
        resource_name="technicians",
        priority=10,
        requested_at=NOW,
        sequence=1,
    )
    reservation = ResourceReservation(
        reservation_id="res-1",
        request_id="wo-0",
        resource_name="technicians",
        acquired_at=NOW,
    )

    with store.transaction() as uow:
        uow.save_resource_definition(definition)
        uow.save_resource_demand(demand)
        uow.save_resource_reservation(reservation)

    assert store.resource_definitions() == (definition,)
    assert store.resource_demands() == (demand,)
    assert store.resource_reservations() == (reservation,)


def test_resource_demands_are_ordered_by_priority_then_sequence():
    store = MemoryPersistence()
    definition = ResourceDefinition("bay", capacity=1)
    with store.transaction() as uow:
        uow.save_resource_definition(definition)
        uow.save_resource_demand(ResourceDemand("normal", "bay", 100, NOW, 1))
        uow.save_resource_demand(ResourceDemand("urgent", "bay", 1, NOW, 3))
        uow.save_resource_demand(ResourceDemand("normal-2", "bay", 100, NOW, 2))

    assert [d.request_id for d in store.resource_demands()] == [
        "urgent",
        "normal",
        "normal-2",
    ]


class RecordingResourceBackend:
    def __init__(self):
        self.created = []
        self.requests = []

    def create_resource(self, name, *, capacity=1):
        self.created.append((name, capacity))

    def request_resource(self, name, *, request_id, on_acquired, priority=100):
        self.requests.append((name, request_id, priority, on_acquired))
        return None


def test_rebuild_restores_reservations_before_pending_demands():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_reservation(
            ResourceReservation("res-holder", "holder", "bay", NOW)
        )
        uow.save_resource_demand(
            ResourceDemand("waiter", "bay", 5, NOW, 1)
        )

    backend = RecordingResourceBackend()
    manager = DurableResourceManager(store)

    restored = manager.rebuild_backend(backend)

    assert restored == 2
    assert backend.created == [("bay", 1)]
    assert [(name, request_id, priority) for name, request_id, priority, _ in backend.requests] == [
        ("bay", "holder", 0),
        ("bay", "waiter", 5),
    ]


def test_backend_grant_atomically_moves_demand_to_reservation():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_demand(ResourceDemand("wo-1", "bay", 10, NOW, 1))

    manager = DurableResourceManager(store)
    reservation = manager.commit_grant(
        request_id="wo-1",
        acquired_at=NOW,
    )

    assert reservation.request_id == "wo-1"
    assert store.resource_demands() == ()
    assert store.resource_reservations() == (reservation,)


def test_duplicate_grant_does_not_create_second_reservation():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_demand(ResourceDemand("wo-1", "bay", 10, NOW, 1))

    manager = DurableResourceManager(store)
    first = manager.commit_grant(request_id="wo-1", acquired_at=NOW)
    second = manager.commit_grant(request_id="wo-1", acquired_at=NOW)

    assert second == first
    assert store.resource_reservations() == (first,)


def test_release_removes_reservation_without_touching_waiters():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_reservation(ResourceReservation("res-1", "holder", "bay", NOW))
        uow.save_resource_demand(ResourceDemand("waiter", "bay", 10, NOW, 1))

    backend = CapacityBackend()
    manager = DurableResourceManager(store)
    manager.rebuild_backend(backend)
    assert manager.release(backend, "res-1") is True

    assert store.resource_reservations() == ()
    assert [d.request_id for d in store.resource_demands()] == ["waiter"]


def test_rebuilt_pending_demand_becomes_durable_reservation_when_backend_grants():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_demand(ResourceDemand("wo-1", "bay", 10, NOW, 7))

    backend = RecordingResourceBackend()
    manager = DurableResourceManager(store)
    manager.rebuild_backend(backend)

    _, _, _, callback = backend.requests[0]
    callback(
        ResourceLease(
            lease_id="backend-lease",
            request_id="wo-1",
            resource_name="bay",
            acquired_at=NOW,
        )
    )

    assert store.resource_demands() == ()
    reservation = store.resource_reservations()[0]
    assert reservation.request_id == "wo-1"
    assert reservation.sequence == 7


def test_new_request_sequence_advances_past_existing_reservations():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_reservation(
            ResourceReservation("res-holder", "holder", "bay", NOW, sequence=12)
        )

    backend = RecordingResourceBackend()
    manager = DurableResourceManager(store)

    demand = manager.request(
        backend,
        resource_name="bay",
        request_id="waiter",
        requested_at=NOW,
        priority=100,
    )

    assert demand.sequence == 13
    assert store.resource_demands() == (demand,)


class CapacityBackend:
    def __init__(self):
        self.capacity = {}
        self.active = {}
        self.queues = {}
        self._sequence = 0

    def create_resource(self, name, *, capacity=1):
        self.capacity[name] = capacity
        self.active[name] = {}
        self.queues[name] = []

    def request_resource(self, name, *, request_id, on_acquired, priority=100):
        self._sequence += 1
        entry = (priority, self._sequence, request_id, on_acquired)
        if len(self.active[name]) < self.capacity[name]:
            self._grant(name, entry)
        else:
            self.queues[name].append(entry)
            self.queues[name].sort(key=lambda item: (item[0], item[1]))
        return None

    def release_resource(self, lease):
        lease_id = lease.lease_id if isinstance(lease, ResourceLease) else lease
        for name, active in self.active.items():
            match = next(
                (request_id for request_id, current in active.items() if current.lease_id == lease_id),
                None,
            )
            if match is not None:
                active.pop(match)
                if self.queues[name]:
                    self._grant(name, self.queues[name].pop(0))
                return
        raise KeyError(lease_id)

    def _grant(self, name, entry):
        _, _, request_id, callback = entry
        lease = ResourceLease(
            lease_id=f"lease-{name}-{request_id}",
            request_id=request_id,
            resource_name=name,
            acquired_at=NOW,
        )
        self.active[name][request_id] = lease
        callback(lease)


def test_restart_preserves_capacity_and_next_waiter_on_release():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_reservation(
            ResourceReservation("res-holder", "holder", "bay", NOW, sequence=1)
        )
        uow.save_resource_demand(ResourceDemand("normal", "bay", 100, NOW, 2))
        uow.save_resource_demand(ResourceDemand("urgent", "bay", 1, NOW, 3))

    backend = CapacityBackend()
    manager = DurableResourceManager(store)

    manager.rebuild_backend(backend)

    assert list(backend.active["bay"]) == ["holder"]
    assert [entry[2] for entry in backend.queues["bay"]] == ["urgent", "normal"]

    assert manager.release(backend, "res-holder") is True

    assert list(backend.active["bay"]) == ["urgent"]
    assert [d.request_id for d in store.resource_demands()] == ["normal"]
    assert [r.request_id for r in store.resource_reservations()] == ["urgent"]


def test_resource_release_is_idempotent_after_durable_consumption():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_reservation(
            ResourceReservation("res-holder", "holder", "bay", NOW, sequence=1)
        )

    backend = CapacityBackend()
    manager = DurableResourceManager(store)
    manager.rebuild_backend(backend)

    assert manager.release(backend, "res-holder") is True
    assert manager.release(backend, "res-holder") is False

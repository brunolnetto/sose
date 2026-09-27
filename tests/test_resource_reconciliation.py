from datetime import datetime, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _runtime():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("worker", capacity=1))
    backend = SimPyBackend(origin=NOW)
    resources = DurableResourceManager(persistence)
    resources.rebuild_backend(backend)
    backend.run_until(NOW)
    return persistence, resources, backend


def test_ensure_requested_is_idempotent_after_immediate_grant():
    persistence, resources, backend = _runtime()

    first = resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="job-1",
        requested_at=backend.now,
        priority=10,
    )
    second = resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="job-1",
        requested_at=backend.now,
        priority=10,
    )

    assert first is not None
    assert second == first
    assert resources.has_request("job-1")
    assert resources.reservation_for("job-1") == first
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == (first,)


def test_withdraw_removes_pending_demand_without_touching_holder():
    persistence, resources, backend = _runtime()

    holder = resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="holder",
        requested_at=backend.now,
        priority=1,
    )
    assert holder is not None

    waiter = resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="waiter",
        requested_at=backend.now,
        priority=10,
    )
    assert waiter is None
    assert resources.has_request("waiter")
    assert resources.reservation_for("waiter") is None

    assert resources.withdraw(backend, "waiter") is True
    assert resources.has_request("waiter") is False
    assert resources.reservation_for("holder") == holder
    assert [r.request_id for r in persistence.resource_reservations()] == ["holder"]


def test_withdraw_releases_granted_request_and_is_retry_safe():
    persistence, resources, backend = _runtime()

    reservation = resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="job-1",
        requested_at=backend.now,
    )
    assert reservation is not None

    assert resources.withdraw(backend, "job-1") is True
    assert resources.has_request("job-1") is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()

    assert resources.withdraw(backend, "job-1") is False


def test_withdraw_handles_request_after_pending_demand_becomes_granted():
    persistence, resources, backend = _runtime()

    holder = resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="holder",
        requested_at=backend.now,
        priority=1,
    )
    assert holder is not None
    assert resources.ensure_requested(
        backend,
        resource_name="worker",
        request_id="racing",
        requested_at=backend.now,
        priority=10,
    ) is None

    assert resources.withdraw(backend, "holder") is True
    backend.run_until(backend.now)

    granted = resources.reservation_for("racing")
    assert granted is not None
    assert persistence.resource_demands() == ()

    assert resources.withdraw(backend, "racing") is True
    assert resources.has_request("racing") is False
    assert persistence.resource_reservations() == ()

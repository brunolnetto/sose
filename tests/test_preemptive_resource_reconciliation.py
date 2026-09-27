from datetime import datetime, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.preemption import DurablePreemptiveResourceManager
from sose.core.runtime import PreemptiveResourceDefinition
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _runtime():
    persistence = MemoryPersistence()
    backend = SimPyBackend(origin=NOW)
    resources = DurablePreemptiveResourceManager(persistence)
    resources.define(PreemptiveResourceDefinition("bay", capacity=1))
    resources.rebuild_backend(backend)
    backend.run_until(NOW)
    return persistence, resources, backend


def test_preemptive_ensure_requested_is_idempotent_after_grant():
    persistence, resources, backend = _runtime()

    first = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="routine",
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    second = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="routine",
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )

    assert first is not None
    assert second == first
    assert resources.has_request("routine")
    assert resources.reservation_for("routine") == first
    assert persistence.preemptive_resource_demands() == ()


def test_preemptive_ensure_requested_preserves_pending_waiter():
    persistence, resources, backend = _runtime()

    holder = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="holder",
        requested_at=backend.now,
        priority=10,
        preempt=False,
    )
    assert holder is not None

    waiter = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="waiter",
        requested_at=backend.now,
        priority=20,
        preempt=False,
    )
    assert waiter is None
    assert resources.has_request("waiter")
    assert resources.reservation_for("waiter") is None
    assert [d.request_id for d in persistence.preemptive_resource_demands()] == [
        "waiter"
    ]


def test_preemptive_ensure_requested_records_preemption_and_successor():
    persistence, resources, backend = _runtime()

    holder = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="routine",
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    assert holder is not None

    urgent = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="aog",
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    assert urgent is not None
    assert resources.reservation_for("routine") is None
    assert resources.reservation_for("aog") == urgent
    result = persistence.resource_preemption_results()[0]
    assert result.displaced_request_id == "routine"
    assert result.preempting_request_id == "aog"


def test_preemptive_withdraw_handles_pending_and_reserved_phases():
    persistence, resources, backend = _runtime()

    holder = resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="holder",
        requested_at=backend.now,
        preempt=False,
    )
    assert holder is not None
    assert resources.ensure_requested(
        backend,
        resource_name="bay",
        request_id="waiter",
        requested_at=backend.now,
        preempt=False,
    ) is None

    assert resources.withdraw(backend, "waiter") is True
    assert resources.has_request("waiter") is False

    assert resources.withdraw(backend, "holder") is True
    assert resources.has_request("holder") is False
    assert persistence.preemptive_resource_reservations() == ()
    assert persistence.preemptive_resource_release_intents() == ()

    assert resources.withdraw(backend, "holder") is False

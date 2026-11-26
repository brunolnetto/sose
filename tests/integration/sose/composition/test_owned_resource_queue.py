"""#494/#496: a component job's own resource queue, attributed in the snapshot.

``ResourceDemand``, ``ResourceReservation`` and ``ResourceReleaseIntent`` carry an
optional ``owner_id``. An organization-attested job stamps its Engine owner on the
records it creates, and the owner is carried from demand to reservation to release
intent. A standalone job stamps nothing, and the codec omits ``None``, so its bytes
are unchanged. The snapshot lists each component's stamped records.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sose.backends.simpy import SimPyBackend
from sose.composition.organization_jobs import build_attested_component_jobs
from sose.composition.organization_snapshot import snapshot_organization
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.persistence.codec import dumps
from sose.persistence.memory import MemoryPersistence
from tests.support import organization_workers as org

T0 = datetime(2026, 11, 26, 8, tzinfo=timezone.utc)


def _run_attested(store):
    jobs = build_attested_component_jobs(org.definition(), store, backend_factory=org._backend,
                                         worker_id="w", **org.admission())
    for job in jobs:
        job.runner.initialize({})
        for index in range(org.TRIGGERS):
            job.runner.run_trigger(trigger_id=org.trigger_id(job.scheduled.job_id, index),
                                   triggered_at=org.T0)


def _record_ids(store) -> set[tuple[str, str]]:
    return {
        *((d.owner_id, d.request_id) for d in store.resource_demands()),
        *((r.owner_id, r.reservation_id) for r in store.resource_reservations()),
        *((i.owner_id, i.intent_id) for i in store.resource_release_intents()),
    }


def test_an_attested_job_stamps_its_queue_and_the_snapshot_lists_it():
    store = MemoryPersistence()
    _run_attested(store)
    stamped = _record_ids(store)
    assert stamped, "the fixture's jobs must use their resource queue"
    owners = {component.owner_id for component in org.definition().components}
    assert {owner for owner, _ in stamped} <= owners
    snapshot = snapshot_organization(org.definition(), store, **org.admission())
    listed = {(c.owner_id, r.record_id) for c in snapshot.components for r in c.resource_queue}
    assert listed == stamped


def test_the_owner_travels_from_demand_to_reservation_to_release_intent(monkeypatch):
    from sose.persistence.memory_uow_resources import ResourceRecordsUnitOfWorkMixin

    saved = []
    original = ResourceRecordsUnitOfWorkMixin.save_resource_release_intent
    monkeypatch.setattr(ResourceRecordsUnitOfWorkMixin, "save_resource_release_intent",
                        lambda self, intent: (saved.append(intent), original(self, intent))[1])
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("dock", capacity=1))
    backend = SimPyBackend(origin=T0)
    manager = DurableResourceManager(store, owner_id="org:owner")
    manager.rebuild_backend(backend)
    manager.request(backend, resource_name="dock", request_id="a", requested_at=T0)
    assert store.resource_demands()[0].owner_id == "org:owner"
    backend.run_until(backend.now)
    (reservation,) = store.resource_reservations()
    assert reservation.owner_id == "org:owner"
    manager.release(backend, reservation.reservation_id)
    assert [intent.owner_id for intent in saved] == ["org:owner"]
    assert store.resource_reservations() == ()


def test_a_standalone_queue_keeps_its_bytes_and_stays_unlisted():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("dock", capacity=1))
    backend = SimPyBackend(origin=T0)
    manager = DurableResourceManager(store)
    manager.rebuild_backend(backend)
    manager.request(backend, resource_name="dock", request_id="a", requested_at=T0)
    backend.run_until(backend.now)
    (reservation,) = store.resource_reservations()
    assert reservation.owner_id is None and "owner_id" not in dumps(reservation)

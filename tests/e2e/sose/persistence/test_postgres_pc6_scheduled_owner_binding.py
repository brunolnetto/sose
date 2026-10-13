"""Independent PostgreSQL workers bind same-type ScheduledWork to distinct owners.

This certifies durable immutable routing records only. It is NOT the
per-organization clock, fenced command delivery or physical-recovery gate.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import os

import pytest

from sose.core.runtime import ScheduledWorkOwnerBinding
from sose.examples.logistics.simulation import seed_reference
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


def test_two_pg_workers_persist_separate_same_type_owner_bindings():
    assert DSN is not None
    namespace = "pc6_work_owner_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        a = seed_reference(store, instance_key="organization-a")
        b = seed_reference(store, instance_key="organization-b")
        assert a.shipment_id != b.shipment_id
        work_for_entity = {
            store.command(work.command_id).entity_id: work.work_id
            for work in store.scheduled_work()
        }
        assert set(work_for_entity) == {a.shipment_id, b.shipment_id}

    first_ready = Barrier(2)

    def bind(org, job, shipment_id):
        with PostgresPersistence(DSN, namespace=namespace) as store:
            binding = ScheduledWorkOwnerBinding(
                work_id=work_for_entity[shipment_id],
                organization_id=org, job_id=job,
            )
            first_ready.wait(timeout=10)
            with store.transaction() as uow:
                uow.bind_scheduled_work_owner(binding)
            return binding

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(bind, "org-a", "job-a", a.shipment_id)
        fb = pool.submit(bind, "org-b", "job-b", b.shipment_id)
        owned_a, owned_b = fa.result(timeout=15), fb.result(timeout=15)

    with PostgresPersistence(DSN, namespace=namespace) as reopened:
        assert reopened.scheduled_work_owner(owned_a.work_id) == owned_a
        assert reopened.scheduled_work_owner(owned_b.work_id) == owned_b
        assert tuple(
            w.work_id for w in reopened.scheduled_work_for_owner("org-a", "job-a")
        ) == (owned_a.work_id,)
        assert tuple(
            w.work_id for w in reopened.scheduled_work_for_owner("org-b", "job-b")
        ) == (owned_b.work_id,)

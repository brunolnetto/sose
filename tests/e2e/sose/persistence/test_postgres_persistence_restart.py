from __future__ import annotations

from datetime import timedelta
import os
from uuid import uuid4

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.postgres import PostgresPersistence
from sose.testing.restart import restart_reference_runtime


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="SOSE_TEST_POSTGRES_DSN is required for PostgreSQL integration tests",
)


def test_postgres_survives_connection_close_reopen_and_backend_rebuild():
    assert DSN is not None
    namespace = f"restart_{uuid4().hex[:16]}"

    first = PostgresPersistence(DSN, namespace=namespace)
    job = seed_job(first)
    _, engine = build_runtime(first)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=2)

    start_and_schedule_completion(
        first,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    first.close()

    reopened = PostgresPersistence(DSN, namespace=namespace)
    rebuilt = restart_reference_runtime(
        reopened,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(complete_at)

    restored = reopened.entity("tutorial_job", job.id)
    assert restored is not None and restored.state == "completed"
    assert reopened.scheduled_work() == ()
    reopened.close()

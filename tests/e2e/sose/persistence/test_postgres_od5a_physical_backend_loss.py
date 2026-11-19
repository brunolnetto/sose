"""#477 real PostgreSQL backend-session death during native physical grant.

The fault is delivered by a second PostgreSQL session with
pg_terminate_backend(), after the real shared booking exists in the writer's
uncommitted SQL transaction. This does not simulate a whole-server restart,
network partition, or post-COMMIT acknowledgment loss.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import psycopg
import pytest

from sose.organizational.od5a_recurring import SharedPhysicalGrantAuthority
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_od5a_shared_native_physical_authority import (
    _binding, _seed, _source, _tick,
)

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL required")


def test_server_terminates_writer_after_physical_insert_before_source_commit(
    monkeypatch,
):
    """An actual SQL backend death cannot create a committed ghost grant."""
    assert DSN is not None
    namespace = "od5a_pg_terminated_" + uuid4().hex[:12]
    aborted, peer = "org:backend-terminated", "org:unaffected-peer"
    binding = _binding("unit-1")
    inserted = Event()
    continue_after_termination = Event()
    writer_pid: list[int] = []
    original_apply = SharedPhysicalGrantAuthority.apply

    def apply_until_backend_death(
        self, *, before, releases, grants, after_state, at, uow,
    ):
        original_apply(
            self, before=before, releases=releases, grants=grants,
            after_state=after_state, at=at, uow=uow,
        )
        if self.owner_id != aborted:
            return
        assert len(grants) == 1
        bookings = self.ledger.reservations()
        assert len(bookings) == 1 and bookings[0].owner_id == aborted
        # Real backend identifier, obtained inside the uncommitted INSERT.
        writer_pid.append(int(self.ledger._db.execute(
            "SELECT pg_backend_pid()",
        ).fetchone()[0]))
        inserted.set()
        if not continue_after_termination.wait(timeout=30):
            raise RuntimeError("termination controller did not release worker")
        # A successful read here indicates the server backend was NOT killed.
        self.ledger._db.execute("SELECT 1")

    with PostgresPersistence(DSN, namespace=namespace) as seed:
        _seed(seed, aborted, binding)
        _seed(seed, peer, binding)
    monkeypatch.setattr(
        SharedPhysicalGrantAuthority, "apply", apply_until_backend_death,
    )

    def run_victim():
        with PostgresPersistence(DSN, namespace=namespace) as store:
            return _tick(store, aborted, binding)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_victim)
        try:
            assert inserted.wait(timeout=45), "victim never inserted physical booking"
            assert len(writer_pid) == 1
            with psycopg.connect(DSN, autocommit=True) as controller:
                controller_pid = int(controller.execute(
                    "SELECT pg_backend_pid()",
                ).fetchone()[0])
                assert writer_pid[0] != controller_pid
                killed = controller.execute(
                    "SELECT pg_terminate_backend(%s)",
                    (writer_pid[0],),
                ).fetchone()[0]
                assert killed is True, "server did not terminate victim backend"
        finally:
            continue_after_termination.set()
        with pytest.raises(psycopg.Error):
            future.result(timeout=45)

    monkeypatch.setattr(SharedPhysicalGrantAuthority, "apply", original_apply)
    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        ledger = recovered.temporal_resources()
        assert ledger.reservations() == ()
        assert _source(recovered, aborted)["step"] == 0
        assert recovered.job_state(aborted).run_count == 0
        assert not any(event.owner_id == aborted for event in recovered.events())
        assert ledger.audit()
        assert _tick(recovered, peer, binding).action == "advanced"
        assert _source(recovered, peer)["step"] == 1
        assert _source(recovered, aborted)["step"] == 0
        assert ledger.audit()

    with PostgresPersistence(DSN, namespace=namespace) as separately_reopened:
        physical = separately_reopened.temporal_resources()
        bookings = physical.reservations()
        assert len(bookings) == 1
        assert bookings[0].owner_id == peer
        assert bookings[0].address.instance_id == "unit-1"
        assert separately_reopened.job_state(aborted).run_count == 0
        assert separately_reopened.job_state(peer).run_count == 1
        assert physical.audit()

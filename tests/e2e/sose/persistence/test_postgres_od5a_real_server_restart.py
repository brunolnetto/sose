"""#477: isolated real PostgreSQL postmaster SIGKILL and WAL restart recovery.

Opt-in Docker/PG test for a *known committed* native OD-5a physical step.
It does not model a true network partition or an unknown server-side COMMIT.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import timedelta
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od5a_recurring import run_od5a_recurring_tick
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_od5a_shared_native_physical_authority import (
    NOW, _binding, _seed, _source, _tick,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SOSE_RUN_PG_SERVER_RESTART") != "1",
    reason="isolated full PG server restart gate must be explicitly enabled",
)


def _docker(*args: str) -> str:
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.STDOUT, timeout=90,
    ).strip()


def _ready(dsn: str) -> None:
    deadline = monotonic() + 50
    last = None
    while monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            return
        except (psycopg.Error, OSError) as exc:
            last = exc
            sleep(0.4)
    pytest.fail(f"PostgreSQL postmaster did not become ready: {last!r}")


def test_hard_killed_postmaster_recovers_one_native_physical_commit():
    """Kill the whole real PG server, replay exactly once, release correctly."""
    if shutil.which("docker") is None:
        pytest.fail("Docker is required: skip is not server-crash certification")
    assert _docker("version", "--format", "{{.Server.Version}}")
    container = "sose-477-restart-" + uuid4().hex[:12]
    org = "org:postmaster-crash"
    binding = _binding("unit-1")
    namespace = "od5a_pg_server_" + uuid4().hex[:12]
    try:
        _docker(
            "run", "-d", "--name", container,
            "-e", "POSTGRES_USER=postgres",
            "-e", "POSTGRES_PASSWORD=postgres",
            "-e", "POSTGRES_DB=sose",
            "-p", "127.0.0.1::5432", "postgres:18",
        )
        exposed = _docker("port", container, "5432/tcp").splitlines()[0]
        assert exposed.startswith("127.0.0.1:"), exposed
        port = int(exposed.rsplit(":", 1)[1])
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/sose"
        _ready(dsn)

        first_trigger = deterministic_id("od5a-recurring-tick", org, 1)
        with PostgresPersistence(dsn, namespace=namespace) as writer:
            _seed(writer, org, binding)
            assert _tick(writer, org, binding).action == "advanced"
            assert _source(writer, org)["step"] == 1
            events = tuple(writer.events())
            source = _source(writer, org)
            ledger = writer.temporal_resources()
            reservations = ledger.reservations()
            physical_snapshot = ledger.snapshot()
            assert len(reservations) == 1
            assert reservations[0].owner_id == org
            assert writer.job_state(org).run_count == 1
            prior_start = writer._connection.execute(
                "SELECT pg_postmaster_start_time()",
            ).fetchone()[0]

        # Whole container/PostgreSQL postmaster hard-killed, not just its
        # connection. docker start reuses exactly the same PGDATA directory.
        assert _docker("kill", "--signal=KILL", container) == container
        assert _docker("start", container) == container
        # Docker may reallocate an ephemeral host port on container restart.
        # The original DSN is now stale even though container/PGDATA persist.
        restarted_port = _docker("port", container, "5432/tcp").splitlines()[0]
        assert restarted_port.startswith("127.0.0.1:"), restarted_port
        new_port = int(restarted_port.rsplit(":", 1)[1])
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{new_port}/sose"
        _ready(dsn)

        with PostgresPersistence(dsn, namespace=namespace) as recovered:
            new_start = recovered._connection.execute(
                "SELECT pg_postmaster_start_time()",
            ).fetchone()[0]
            assert new_start > prior_start
            assert _source(recovered, org) == source
            assert tuple(recovered.events()) == events
            assert recovered.temporal_resources().reservations() == reservations
            assert recovered.temporal_resources().snapshot() == physical_snapshot
            assert recovered.temporal_resources().audit()
            assert recovered.job_state(org).run_count == 1

            # Replay the first exact trigger: no new source step or booking.
            replay = run_od5a_recurring_tick(
                recovered, job_id=org, owner_id="worker:server-recovery",
                now=NOW, lease_duration=timedelta(seconds=60),
                trigger_id=first_trigger, shared_pool_binding=binding,
            )
            assert replay.action == "replayed"
            assert _source(recovered, org) == source
            assert tuple(recovered.events()) == events
            assert recovered.temporal_resources().reservations() == reservations

            # The recovered server can persist the *next real native tick*.
            assert _tick(recovered, org, binding).action == "advanced"
            assert _source(recovered, org)["step"] == 2
            assert recovered.job_state(org).run_count == 2
            released = recovered.temporal_resources().reservations()
            assert len(released) == 1
            assert released[0].status == "released"
            assert recovered.temporal_resources().audit()
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True, text=True, timeout=35, check=False,
        )

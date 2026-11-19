"""#477: lose a real PostgreSQL COMMIT reply on native OD-5a physical booking.

This is a *wire-level* lost-successful-COMMIT-reply fault, not a mocked
ConnectionError. Reuses the existing independently checked PostgreSQL proxy
used for canonical PC6 BusinessEffectApplied COMMIT falsifiers.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od5a_recurring import run_od5a_recurring_tick
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_od5a_shared_native_physical_authority import (
    NOW, _binding, _seed, _source, _tick,
)
from tests.e2e.sose.persistence.test_postgres_pc6_uncertain_effect_commit import (
    _start_commit_reply_drop_proxy,
)
from tests.e2e.sose.persistence.test_postgres_infrastructure_chaos import _free_port
from tests.support.chaos_waits import wait_for_marker_exists


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL TCP required")

# The worker uses ONLY the proxied PostgreSQL connection. The parent never
# relies on the exit code to infer COMMIT outcome; it separately reads the
# real authoritative source and physical ledger through its direct DSN.
# Patch applies strictly to the target OD-5a namespace/physical booking.
_WORKER = """
import sys
from pathlib import Path
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_od5a_shared_native_physical_authority import (
    _binding, _tick,
)

dsn, namespace, org, arm = sys.argv[1:]
original = PostgresPersistence._apply_changes
armed = [False]

def after_real_physical_insert(self, before, after, dirty_records):
    result = original(self, before, after, dirty_records)
    if self.namespace != namespace or armed[0]:
        return result
    # Reads inside the SAME transaction, *after* original native source UoW
    # and real temporal INSERT. A no-op/fabricated physical grant fails.
    booked = self.temporal_resources(bootstrap=False).reservations()
    if booked:
        assert len(booked) == 1
        assert booked[0].owner_id == org and booked[0].status == "reserved"
        assert booked[0].address.instance_id == "unit-1"
        armed[0] = True
        Path(arm).write_text("armed_after_real_native_physical_insert")
    return result

PostgresPersistence._apply_changes = after_real_physical_insert
with PostgresPersistence(dsn, namespace=namespace) as store:
    _tick(store, org, _binding("unit-1"))
raise AssertionError("source did not lose its actual PostgreSQL COMMIT reply")
"""


def test_native_physical_source_commit_reply_dropped_but_authority_recovers_once(
    tmp_path,
):
    """Actual successful COMMIT, no client reply, one grant after fresh restart.

    Parent witnesses the real backend CommandComplete(COMMIT) and ReadyForQuery
    only in the independent proxy, then verifies the source, its exactly-once
    trigger and physical capacity on a brand-new direct PostgreSQL connection.
    """
    assert DSN is not None
    ns = "od5a_net_" + uuid4().hex[:12]
    # The '_reference' suffix must stay within PostgreSQL adapter's 40-char bound.
    assert len(ns + "_reference") <= 40
    reference_ns = ns + "_reference"
    org = "org:lost-commit-response"
    peer = "org:physical-competing-peer"
    binding = _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)

    # Two identical, immutable exogenous seed worlds. One advances normally,
    # the other will lose its actual successful database COMMIT reply.
    with PostgresPersistence(DSN, namespace=reference_ns) as reference:
        _seed(reference, org, binding)
        assert _tick(reference, org, binding).action == "advanced"
        reference_source = _source(reference, org)
        reference_events = tuple(reference.events())
        reference_bookings = reference.temporal_resources().reservations()
        assert len(reference_bookings) == 1
    with PostgresPersistence(DSN, namespace=ns) as seed:
        _seed(seed, org, binding)

    proxy = worker = None
    try:
        proxy, proxied_dsn, arm, claimed, dropped = (
            _start_commit_reply_drop_proxy(
                DSN, port=_free_port(), tmp_path=tmp_path,
            )
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER, proxied_dsn, ns, org, str(arm)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_for_marker_exists(
            dropped, proxy, timeout=65,
            premature_exit_label="real COMMIT proxy died before native physical response",
            timeout_label="native physical COMMIT was never intercepted",
            termination_timeout=10,
        )
        assert dropped.read_text(encoding="utf-8") == (
            "commit_complete_response_dropped"
        ), "must witness a real backend COMMIT before discarding client's reply"
        assert claimed.is_file() and not arm.exists(), (
            "proxy did not intercept the armed native physical transaction"
        )
        try:
            out, err = worker.communicate(timeout=22)
        except subprocess.TimeoutExpired:
            worker.kill()
            out, err = worker.communicate(timeout=10)
            pytest.fail("native source worker never observed severed SQL reply")
        assert worker.returncode != 0, (
            "unacknowledged COMMIT was silently accepted", out[-1600:], err[-3000:],
        )
        assert (
            "psycopg" in err.lower()
            and ("connection" in err.lower() or "operationalerror" in err.lower())
        ), ("worker failed for a non-transport reason", out[-1600:], err[-3000:])
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=10)
        if proxy is not None and proxy.poll() is None:
            proxy.kill()
            proxy.communicate(timeout=10)

    with PostgresPersistence(DSN, namespace=ns) as recovered:
        actual_bookings = recovered.temporal_resources().reservations()
        assert actual_bookings == reference_bookings
        assert _source(recovered, org) == reference_source
        assert tuple(recovered.events()) == reference_events
        assert recovered.job_state(org).run_count == 1
        assert recovered.temporal_resources().audit()

        before = (
            _source(recovered, org), tuple(recovered.events()),
            recovered.temporal_resources().snapshot(),
            actual_bookings,
        )
        replay = run_od5a_recurring_tick(
            recovered, job_id=org, owner_id="worker:after-wire-cut",
            now=NOW, lease_duration=timedelta(seconds=60),
            trigger_id=trigger, shared_pool_binding=binding,
        )
        assert replay.action == "replayed"
        after = (
            _source(recovered, org), tuple(recovered.events()),
            recovered.temporal_resources().snapshot(),
            recovered.temporal_resources().reservations(),
        )
        assert after == before, "ambiguous successful COMMIT duplicated causal/physical truth"

        # A truly shared external instance is still occupied. Another owner
        # must wait, rather than inferring a free slot from the first worker's
        # unsuccessful response.
        _seed(recovered, peer, binding)
        assert _tick(recovered, peer, binding).action == "waiting"
        assert _source(recovered, peer)["step"] == 0
        assert recovered.temporal_resources().reservations() == actual_bookings
        assert recovered.temporal_resources().audit()

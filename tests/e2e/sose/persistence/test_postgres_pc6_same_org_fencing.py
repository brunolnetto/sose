"""PC6 adversarial PostgreSQL fencing: actual competing OS processes, one job.

Separate from infrastructure failures. A stale writer must not mutate causal
history even when a successor completed the same durable recurrence.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from sose.composition.scheduler import RecoverySchedule
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_pc6_full_real_domain_recovery import (
    _seed_before_logistics_ack, _policy, _proof, _schedule, _terminal, _tick,
)

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL required")

# A genuinely independent worker takes an ownership epoch, blocks, and then
# tries to write with the *same old epoch* after a newer worker has taken over.
_OLD_WORKER = """
import os, sys, time
from pathlib import Path
from sose.persistence.postgres import PostgresPersistence, StaleWriterError
dsn, namespace, ready_path, release_path = sys.argv[1:]
with PostgresPersistence(dsn, namespace=namespace) as store:
    lease = store.claim_writer("obsolete-a", expected_epoch=store.writer_epoch())
    Path(ready_path).write_text(str(lease.epoch))
    deadline = time.monotonic() + 25
    while not Path(release_path).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("test never released obsolete worker")
        time.sleep(.025)
    try:
        with store.transaction(owner_epoch=lease.epoch) as uow:
            # Even a read transaction must be rejected before its UoW opens.
            uow.get_job_state("real-pc6-a")
    except StaleWriterError:
        sys.exit(42)
    raise AssertionError("obsolete epoch acquired transactional access")
"""


def _wait_file(path: Path, child: subprocess.Popen) -> None:
    import time
    deadline = time.monotonic() + 20
    while not path.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.025)
    assert path.exists(), f"worker died before durable claim: {child.poll()}"


def _completed_state(dsn, namespaces, physical_ns, seeds):
    for org in ("a", "b"):
        for tick in range(1, 26):
            if _terminal(dsn, namespaces[org]):
                break
            _tick(dsn, namespaces[org], physical_ns, org, seeds[org][0], tick)
        assert _terminal(dsn, namespaces[org])
    return _proof(dsn, namespaces, physical_ns, seeds)


def _scenario(*, adversarial: str | None, tmp_path: Path):
    namespaces = {k: f"fence_{k}_{uuid4().hex[:12]}" for k in ("a", "b")}
    physical_ns = f"fence_physical_{uuid4().hex[:12]}"
    seeded = {org: _seed_before_logistics_ack(DSN, ns, org)
              for org, ns in namespaces.items()}
    seeds = {org: values[:2] for org, values in seeded.items()}
    child = None
    ready, release = tmp_path / "ready", tmp_path / "release"
    ready.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    try:
        if adversarial is not None:
            child = subprocess.Popen(
                [sys.executable, "-c", _OLD_WORKER, DSN, namespaces["a"],
                 str(ready), str(release)],
                stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            _wait_file(ready, child)
            assert child.poll() is None
        # B is independent: A's live/suspended owner may not lock B's clock.
        _tick(DSN, namespaces["b"], physical_ns, "b", seeds["b"][0], 0)
        if adversarial == "killed":
            assert child is not None
            child.kill()  # SIGKILL; no destructor/finally in worker A.
            child.communicate(timeout=10)
            assert child.returncode == -signal.SIGKILL
        _tick(DSN, namespaces["a"], physical_ns, "a", seeds["a"][0], 0)
        if adversarial == "zombie":
            assert child is not None
            release.write_text("resume", encoding="utf-8")
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 42, (stdout, stderr)
        with PostgresPersistence(DSN, namespace=namespaces["a"]) as a:
            state = a.job_state("real-pc6-a")
            assert state is not None and state.next_tick == 1
            assert state.active_trigger_id is None
            assert len(state.completed_batch_triggers) == 1
        return _completed_state(DSN, namespaces, physical_ns, seeds)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


@pytest.mark.parametrize("adversarial", ("killed", "zombie"))
def test_same_organization_real_process_takeover_keeps_full_pc6_truth(
    tmp_path, adversarial,
):
    baseline = _scenario(adversarial=None, tmp_path=tmp_path)
    recovered = _scenario(adversarial=adversarial, tmp_path=tmp_path)
    assert recovered == baseline

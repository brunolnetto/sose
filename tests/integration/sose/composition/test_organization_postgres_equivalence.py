"""#496: the composed run on PostgreSQL, with independent OS workers and a worker
killed during a trigger, is equivalent to a reference run on memory.

Two oracles, both against the reference run:
- the canonical organization snapshot (#681), with every chart of both domains
  declared so it attributes every transition of the run;
- the complete durable state the run writes (events per owner, entities with
  version and attributes, resource demands/reservations/release intents,
  simulation positions, full job checkpoints, scheduled work).
"""
from __future__ import annotations

from multiprocessing import get_context
import os
from uuid import uuid4

import pytest

from sose.composition.organization_snapshot import snapshot_organization
from sose.persistence.memory import MemoryPersistence
from tests.support import organization_workers as org

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


def _reference():
    persistence = MemoryPersistence()
    for job_id in (org.FULFILL, org.WAREHOUSE):
        org.run_component(persistence, job_id, "reference")
    return (snapshot_organization(org.definition(), persistence, **org.admission()),
            org.durable_state(persistence))


def _postgres_snapshot(namespace):
    from sose.persistence.postgres import PostgresPersistence

    with PostgresPersistence(DSN, namespace=namespace) as persistence:
        return snapshot_organization(org.definition(), persistence, **org.admission()), {
            job_id: persistence.job_state(job_id) for job_id in (org.FULFILL, org.WAREHOUSE)
        }


def _assert_equivalent(namespace):
    from sose.persistence.postgres import PostgresPersistence

    reference_snapshot, reference_state = _reference()
    snapshot, _ = _postgres_snapshot(namespace)
    assert snapshot == reference_snapshot
    with PostgresPersistence(DSN, namespace=namespace) as persistence:
        assert org.durable_state(persistence) == reference_state


def _spawn(namespace, job_id, worker, **kwargs):
    process = get_context("spawn").Process(
        target=org.component_worker, args=(DSN, namespace, job_id, worker), kwargs=kwargs,
    )
    process.start()
    return process


def _join(*processes):
    for process in processes:
        process.join(timeout=120)
        if process.is_alive():
            process.kill()
            process.join()
            pytest.fail("component worker hung")


def test_concurrent_component_workers_match_the_reference_run():
    namespace = f"orgeq_{uuid4().hex[:12]}"
    workers = (
        _spawn(namespace, org.FULFILL, "worker-fulfill"),
        _spawn(namespace, org.WAREHOUSE, "worker-warehouse"),
    )
    _join(*workers)
    assert [worker.exitcode for worker in workers] == [0, 0]
    _assert_equivalent(namespace)
    _, states = _postgres_snapshot(namespace)
    assert {state.run_count for state in states.values()} == {org.TRIGGERS}


# Trigger 2 of the fulfillment job commits 25 transactions. Its single tick completes
# before the last few, which close the batch.
@pytest.mark.parametrize("crash_commits, stage", [
    (1, "tick-open"),        # right after the first commit
    (12, "tick-open"),       # mid-tick
    (24, "batch-closing"),   # tick completed, batch still open
])
def test_a_worker_killed_during_a_trigger_is_resumed_to_the_reference_state(
    crash_commits, stage,
):
    namespace = f"orgeq_{uuid4().hex[:12]}"
    crashed = _spawn(namespace, org.FULFILL, "worker-fulfill-1", crash_trigger=2,
                     crash_commits=crash_commits)
    warehouse = _spawn(namespace, org.WAREHOUSE, "worker-warehouse")
    _join(crashed, warehouse)
    assert (crashed.exitcode, warehouse.exitcode) == (org.CRASH_EXIT, 0)

    _, states = _postgres_snapshot(namespace)
    interrupted = states[org.FULFILL]
    open_ids = (interrupted.active_trigger_id, interrupted.active_batch_trigger_id)
    if stage == "tick-open":
        # The occurrence is open (at commit 1 only its batch claim has committed).
        assert interrupted.run_count == 2
        assert org.trigger_id(org.FULFILL, 2) in open_ids
        # Without explicit recovery the replacement is refused, for that reason,
        # and commits no job progress (its lease claim still moves the epoch).
        refused = _spawn(namespace, org.FULFILL, "worker-fulfill-2")
        _join(refused)
        assert refused.exitcode == org.REFUSED_EXIT
        assert _postgres_snapshot(namespace)[1][org.FULFILL] == interrupted
        recovery = {"recover_trigger": 2}
    else:
        # The tick completed and must not run again; only the batch is left open,
        # and closing it needs no explicit recovery.
        assert interrupted.run_count == 3
        assert open_ids == (None, org.trigger_id(org.FULFILL, 2))
        recovery = {}

    replacement = _spawn(namespace, org.FULFILL, "worker-fulfill-3", **recovery)
    _join(replacement)
    assert replacement.exitcode == 0
    _assert_equivalent(namespace)
    assert _postgres_snapshot(namespace)[1][org.FULFILL].run_count == org.TRIGGERS

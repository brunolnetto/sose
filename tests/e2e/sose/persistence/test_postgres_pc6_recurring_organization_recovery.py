"""PC6: independently fenced recurring jobs and organizational restart clocks.

A global namespace writer epoch must not invalidate another organization's
in-flight job when the two operate on disjoint state and share only genuinely
finite physical resource pools.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import os

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.postgres import PostgresPersistence, StaleWriterError

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _namespace():
    return "pc6_recur_" + uuid4().hex[:12]


def test_scoped_pg_epochs_fence_only_the_same_job():
    assert DSN
    ns = _namespace()
    with (
        PostgresPersistence(DSN, namespace=ns) as first,
        PostgresPersistence(DSN, namespace=ns) as second,
        PostgresPersistence(DSN, namespace=ns) as successor,
    ):
        a = first.claim_scoped_writer(
            "pc6-job-organization-a", "worker-a",
            expected_epoch=first.scoped_writer_epoch("pc6-job-organization-a"),
        )
        b = second.claim_scoped_writer(
            "pc6-job-organization-b", "worker-b",
            expected_epoch=second.scoped_writer_epoch("pc6-job-organization-b"),
        )
        with FencedEnginePersistence(first, a).transaction() as uow:
            assert uow.get_job_state("pc6-job-organization-a") is None
        with FencedEnginePersistence(second, b).transaction() as uow:
            assert uow.get_job_state("pc6-job-organization-b") is None
        successor.claim_scoped_writer(
            "pc6-job-organization-a", "worker-a-recovered",
            expected_epoch=a.epoch,
        )
        with pytest.raises(StaleWriterError, match="stale scoped writer"):
            with FencedEnginePersistence(first, a).transaction():
                pass
        # The unrelated organization was not fenced out by recovery of A.
        with FencedEnginePersistence(second, b).transaction():
            pass


def test_crash_during_recurrence_replays_only_its_job_slot(monkeypatch):
    assert DSN
    ns = _namespace()
    slots = {"organization-a": T0, "organization-b": T0 + timedelta(hours=2)}
    observations = []
    failure = {"armed": True}

    def bounded(self, store, *, now, logical_now=None):
        scope = store.lease.scope
        observations.append((scope, now))
        if scope == "job-a" and failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated worker death after durable checkpoint")
        return 1

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", bounded)

    def job(store, org, job_id, owner):
        return RecoverySchedule(
            runner=TradingCustomerRecoveryRunner(
                persistence=store, owner_id=owner, job_id=job_id,
                correlation_id=org, scoped_writer=True,
            ),
            start_at=slots[org], interval=timedelta(minutes=5),
            max_slots=2,
        )

    with PostgresPersistence(DSN, namespace=ns) as failed_a:
        with pytest.raises(RuntimeError, match="simulated worker death"):
            job(failed_a, "organization-a", "job-a", "worker-a").run_due(now=T0)
        checkpoint = failed_a.job_state("job-a")
        assert checkpoint is not None
        assert checkpoint.active_trigger_id is not None
        assert checkpoint.logical_time == T0

    # B is allowed to advance its own time even while A's slot remains active.
    with PostgresPersistence(DSN, namespace=ns) as healthy_b:
        results = job(healthy_b, "organization-b", "job-b", "worker-b").run_due(
            now=slots["organization-b"],
        )
        assert len(results) == 1 and results[0].actions == 1
        assert healthy_b.job_state("job-b").logical_time == slots["organization-b"]
        assert healthy_b.job_state("job-a").active_trigger_id is not None

    with PostgresPersistence(DSN, namespace=ns) as recovered_a:
        results = job(recovered_a, "organization-a", "job-a", "worker-a-recovered").run_due(
            now=T0 + timedelta(minutes=5),
        )
        assert len(results) == 2
        assert recovered_a.job_state("job-a").logical_time == T0 + timedelta(minutes=5)
        assert recovered_a.job_state("job-b").logical_time == slots["organization-b"]
        assert recovered_a.job_state("job-a").active_trigger_id is None
        assert recovered_a.job_state("job-b").active_trigger_id is None
        assert recovered_a.job_state("job-a").run_count == 2
        assert recovered_a.job_state("job-b").run_count == 1
        assert observations == [
            ("job-a", T0), ("job-b", slots["organization-b"]),
            ("job-a", T0), ("job-a", T0 + timedelta(minutes=5)),
        ]


def test_two_distinct_job_scopes_can_be_in_flight_together(monkeypatch):
    assert DSN
    ns = _namespace()
    barrier = Barrier(2)

    def bounded(self, store, *, now, logical_now=None):
        barrier.wait(timeout=12)
        with store.transaction() as uow:
            own = uow.get_job_state(self.job_id)
            other = uow.get_job_state("job-b" if self.job_id == "job-a" else "job-a")
            assert own is not None and own.active_trigger_id is not None
            assert other is not None and other.active_trigger_id is not None
        return 0

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", bounded)

    def worker(key):
        with PostgresPersistence(DSN, namespace=ns) as store:
            runner = TradingCustomerRecoveryRunner(
                persistence=store, owner_id=f"worker-{key}",
                job_id=f"job-{key}", scoped_writer=True, correlation_id=f"org-{key}",
            )
            return runner.run_scheduled_trigger(
                scheduled_for=T0 + timedelta(minutes=5 if key == "b" else 0),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, key) for key in ("a", "b")]
        results = [future.result(timeout=25) for future in futures]
    assert {result.job_id for result in results} == {"job-a", "job-b"}
    with PostgresPersistence(DSN, namespace=ns) as recovered:
        assert recovered.job_state("job-a").run_count == 1
        assert recovered.job_state("job-b").run_count == 1

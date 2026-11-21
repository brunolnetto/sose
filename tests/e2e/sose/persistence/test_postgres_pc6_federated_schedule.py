"""Each organization has its own durable trigger cursor and failure domain.

PostgreSQL policy-mode port of the deprecated SQLite recovery (#634); restart
is a fresh PostgresPersistence connection on the same namespace.
"""
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import OrganizationRecoveryFleet, RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")

# Certified, but never issued here (these schedules run on empty ledgers):
# mapping it only selects policy mode without changing admission (#634).
NEUTRAL_POLICY = {
    "composition.post_replenishment_journal": ResourcePoolContract(
        resource_type="journal_clerk", pool_id="pc6-federated-schedule-neutral",
        scope="shared", capacity=1,
    ),
}


START = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def _schedule(store, organization):
    return RecoverySchedule(
        TradingCustomerRecoveryRunner(
            persistence=store, owner_id=f"worker-{organization}",
            job_id=f"pc6-{organization}", max_actions=1,
            correlation_id=f"pc6-fixture-{organization}",
            resource_policies=NEUTRAL_POLICY,
            # Explicit fixture binding; no flow exists on these empty ledgers.
            resource_organizations={f"pc6-fixture-{organization}": f"test-org-{organization}"},
        ),
        START, timedelta(minutes=1), max_slots=2,
    )


def test_fleet_restarts_failed_organization_without_rewinding_an_independent_job(
    monkeypatch,
):
    run = uuid4().hex[:12]
    a_ns, b_ns = f"pc6_federated_a_{run}", f"pc6_federated_b_{run}"
    original = TradingCustomerRecoveryRunner._run_bounded

    def fail_org_a(self, store, *, now, logical_now=None):
        if self.job_id == "pc6-a":
            raise RuntimeError("injected death after durable trigger claim")
        return original(self, store, now=now, logical_now=logical_now)

    with PostgresPersistence(DSN, namespace=a_ns) as a, \
            PostgresPersistence(DSN, namespace=b_ns) as b:
        fleet = OrganizationRecoveryFleet({"a": _schedule(a, "a"), "b": _schedule(b, "b")})
        monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", fail_org_a)
        result = fleet.run_due(now=START)
        assert set(result.failures) == {"a"}
        assert "RuntimeError" in result.failures["a"]
        assert len(result.completed["b"]) == 1
        assert a.job_state("pc6-a").active_trigger_id is not None
        assert b.job_state("pc6-b").next_tick == 1

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", original)
    with PostgresPersistence(DSN, namespace=a_ns) as a, \
            PostgresPersistence(DSN, namespace=b_ns) as b:
        fleet = OrganizationRecoveryFleet({"a": _schedule(a, "a"), "b": _schedule(b, "b")})
        result = fleet.run_due(now=START + timedelta(minutes=1))
        assert not result.failures
        assert len(result.completed["a"]) == 2  # incomplete 0, then 1
        assert len(result.completed["b"]) == 1  # only slot 1
        assert a.job_state("pc6-a").next_tick == 2
        assert b.job_state("pc6-b").next_tick == 2
        assert fleet.run_due(now=START + timedelta(minutes=1)).completed == {}


def test_fleet_rejects_shared_operational_ownership():
    with PostgresPersistence(DSN, namespace="pc6_federated_one_" + uuid4().hex[:12]) as store:
        first = _schedule(store, "a")
        second = _schedule(store, "b")
        with pytest.raises(ValueError, match="distinct"):
            OrganizationRecoveryFleet({"a": first, "b": second})

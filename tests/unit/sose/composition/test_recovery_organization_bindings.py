"""PC6 recovery must resolve real organizations independently of worker leases."""
import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

# Neutral certified policy (#634): construction only; no recovery pass runs here.
POLICIES = {
    "composition.settle_customer_payment": ResourcePoolContract(
        resource_type="settlement_slot", pool_id="neutral-bindings",
        scope="shared", capacity=1,
    ),
}


def test_recovery_uses_explicit_correlation_to_organization_binding(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "owners.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id="ephemeral-worker",
            organization_owners={
                "correlation-a": "organization-a", "correlation-b": "organization-b",
            },
            resource_policies=POLICIES,
            resource_organizations={"correlation-a": "organization-a"},
        )
        assert runner._organization_for("correlation-a") == "organization-a"
        assert runner._organization_for("correlation-b") == "organization-b"
        with pytest.raises(ValueError, match="explicit correlation-to-organization"):
            runner._organization_for("correlation-c")


def test_recovery_never_infers_organization_from_worker_lease(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "missing.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id="lease-owner-is-not-organization",
            resource_policies=POLICIES,
            # Policy mode requires some binding; the queried correlation stays unbound.
            resource_organizations={"correlation-b": "organization-b"},
        )
        with pytest.raises(ValueError, match="explicit correlation-to-organization"):
            runner._organization_for("correlation-a")


def test_recovery_rejects_conflicting_physical_and_runtime_owners(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "conflict.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id="worker",
            organization_owners={"correlation-a": "organization-a"},
            resource_organizations={"correlation-a": "organization-b"},
            resource_policies=POLICIES,
        )
        with pytest.raises(ValueError, match="bindings disagree"):
            runner._organization_for("correlation-a")

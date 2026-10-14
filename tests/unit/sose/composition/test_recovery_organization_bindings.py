"""PC6 recovery must resolve real organizations independently of worker leases."""
import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def test_recovery_uses_explicit_correlation_to_organization_binding(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "owners.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id="ephemeral-worker",
            organization_owners={"correlation-a": "organization-a", "correlation-b": "organization-b"},
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
        )
        with pytest.raises(ValueError, match="bindings disagree"):
            runner._organization_for("correlation-a")

"""Construction-time validation of the PC6 recovery runner (split from recovery.py)."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from sose.persistence.ownership import has_durable_writer_fencing

if TYPE_CHECKING:
    from sose.composition.recovery import TradingCustomerRecoveryRunner


def _validate_identity(runner: TradingCustomerRecoveryRunner) -> None:
    if not runner.owner_id or not runner.job_id:
        raise ValueError("worker owner and job identity must be nonempty")
    if runner.max_actions < 1:
        raise ValueError("max_actions must be >= 1")
    if runner.lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")
    if runner.correlation_id is not None and not runner.correlation_id:
        raise ValueError("correlation_id cannot be empty")


def _validate_scoped_writer(runner: TradingCustomerRecoveryRunner) -> None:
    if runner.scoped_writer is not True:
        # #635: deprecated in 0.13.0, removed in 0.14.0 (ADR-0011).
        raise TypeError(
            "TradingCustomerRecoveryRunner runs only under its job-scoped writer lease; "
            "the namespace-wide lease (scoped_writer=False) was removed in SOSE 0.14.0. "
            "Re-bind an existing job once with sose composition-recovery --rebind-scoped"
        )
    if runner.correlation_id is None:
        raise ValueError("scoped recovery requires one explicit correlation_id")
    if not has_durable_writer_fencing(runner.persistence, "claim_scoped_writer"):
        raise TypeError("scoped recovery requires durable scoped writer fencing")


def _validate_resource_persistence(runner: TradingCustomerRecoveryRunner) -> None:
    if runner.resource_persistence is not None:
        if (not hasattr(runner.persistence, "dsn")
            or runner.persistence.dsn != getattr(runner.resource_persistence, "dsn", None)):
            raise ValueError("domain and physical resource ledgers must share PostgreSQL")


def _validate_organization_owners(runner: TradingCustomerRecoveryRunner) -> None:
    if runner.organization_owners is not None and any(
        not isinstance(correlation, str) or not correlation.strip()
        or not isinstance(organization, str) or not organization.strip()
        for correlation, organization in runner.organization_owners.items()
    ):
        raise ValueError("organization_owners must contain valid durable bindings")


def _validate_resource_policies(runner: TradingCustomerRecoveryRunner) -> None:
    if runner.resource_policies is None:
        # #634: deprecated in 0.9.0, removed in 0.11.0 (ADR-0011).
        raise TypeError(
            "TradingCustomerRecoveryRunner requires resource_policies and "
            "resource_organizations (PostgreSQL-backed recovery); policy-less "
            "recovery was removed in SOSE 0.11.0"
        )
    if (runner.resource_slot_duration <= timedelta(0)
            or runner.resource_retry_delay <= timedelta(0)):
        raise ValueError("resource slot and retry delay must be positive")
    if not runner.resource_organizations or any(
        not correlation or not organization
        for correlation, organization in runner.resource_organizations.items()
    ):
        raise ValueError(
            "resource policies require explicit correlation-to-organization bindings"
        )


def _validate_owned_correlation(runner: TradingCustomerRecoveryRunner) -> None:
    if runner.correlation_id is not None:
        if runner.correlation_id not in runner.resource_organizations:
            raise ValueError("resource mapping lacks the owned correlation_id")


def validate_recovery_runner(runner: TradingCustomerRecoveryRunner) -> None:
    """Run every construction check in the original order; the first failure raises."""
    _validate_identity(runner)
    _validate_scoped_writer(runner)
    _validate_resource_persistence(runner)
    _validate_organization_owners(runner)
    _validate_resource_policies(runner)
    _validate_owned_correlation(runner)

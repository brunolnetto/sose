"""Construction-time validation of the PC6 recovery runner (split from recovery.py)."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

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
    if runner.scoped_writer:
        if runner.correlation_id is None:
            raise ValueError("scoped recovery requires one explicit correlation_id")
        if not callable(getattr(runner.persistence, "claim_scoped_writer", None)):
            raise TypeError("scoped recovery requires PostgreSQL scoped fencing")


def _validate_resource_persistence(runner: TradingCustomerRecoveryRunner) -> None:
    if runner.resource_persistence is not None:
        if runner.resource_policies is None:
            raise ValueError("shared resource persistence requires explicit policies")
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
    if runner.resource_policies is not None:
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
    if runner.resource_policies is not None and runner.correlation_id is not None:
        if runner.correlation_id not in runner.resource_organizations:
            raise ValueError("resource mapping lacks the owned correlation_id")


def _validate_writer_fencing(runner: TradingCustomerRecoveryRunner) -> None:
    if not runner.scoped_writer and (
        not callable(getattr(runner.persistence, "writer_epoch", None))
        or not callable(getattr(runner.persistence, "claim_writer", None))
    ):
        raise TypeError("composition runner requires authoritative writer fencing")


def validate_recovery_runner(runner: TradingCustomerRecoveryRunner) -> None:
    """Run every construction check in the original order; the first failure raises."""
    _validate_identity(runner)
    _validate_scoped_writer(runner)
    _validate_resource_persistence(runner)
    _validate_organization_owners(runner)
    _validate_resource_policies(runner)
    _validate_owned_correlation(runner)
    _validate_writer_fencing(runner)

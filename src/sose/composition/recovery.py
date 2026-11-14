"""Bounded, restartable PC6 customer-boundary recovery trigger.

This runner owns no business entities. It replays accepted domain intents,
reconstructs outbound WF messages from durable facts, and applies WM consumption
through the existing boundary registry. It now resumes Logistics effects from accepted dispatches. O2C/Payments/R2R
still require separate domain worker ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, replace  # noqa: F401
from datetime import datetime, timedelta
import json  # noqa: F401
from typing import Mapping

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import TemporalReservation  # noqa: F401
from sose.composition.resource_intents import IntentResourceCoordinator

from sose.composition import trading_company_customer as customer  # noqa: F401
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService  # noqa: F401
from sose.composition.model import BoundaryMessage, DeliveryStatus  # noqa: F401
from sose.composition.recovery_boundary import (  # noqa: F401
    _LOGISTICS_CONTRACT,
    _O2C_CONTRACT,
    _PAYMENTS_CONTRACT,
    _R2R_CONTRACT,
    _RECOVERABLE,
    _WM_CONTRACT,
    execute_pending,
    pending_effects,
    registry_for,
)
from sose.composition.recovery_pass import run_bounded_pass
from sose.composition.recovery_trigger import (
    activate_trigger,
    check_job_identity,
    claim_trigger_writer,
    complete_trigger,
    trigger_config_json,
)
from sose.composition.recovery_validation import validate_recovery_runner
from sose.examples.warehouse_fulfillment import simulation as fulfillment  # noqa: F401
from sose.examples.logistics import simulation as logistics  # noqa: F401
from sose.examples.order_to_cash import simulation as o2c  # noqa: F401
from sose.examples.cards_payments import simulation as payments  # noqa: F401
from sose.examples.record_to_report import simulation as r2r  # noqa: F401
from sose.jobs.model import CompletedJobTrigger, SimulationJobState  # noqa: F401
from sose.jobs.registration import job_registration_guard
from sose.persistence.base import Persistence
from sose.persistence.ownership import FencedEnginePersistence


@dataclass(frozen=True, slots=True)
class RecoveryTriggerResult:
    job_id: str
    trigger_id: str
    actions: int


@dataclass(slots=True)
class TradingCustomerRecoveryRunner:
    """One trigger is a durable, bounded pass over existing authoritative state.

    Requires ownership-capable EnginePersistence. The externally scheduled
    caller supplies a durable trigger identity, and the writer epoch fences
    stale attempts. The SOSE job-state checkpoint permits restart with the
    same trigger ID after a worker death.
    """

    persistence: Persistence
    owner_id: str
    job_id: str = "trading-company-customer-recovery"
    max_actions: int = 16
    lease_duration: timedelta = timedelta(hours=1)
    resource_policies: Mapping[str, ResourcePoolContract] | None = None
    resource_organizations: Mapping[str, str] | None = None
    organization_owners: Mapping[str, str] | None = None
    resource_persistence: Persistence | None = None
    resource_slot_duration: timedelta = timedelta(minutes=5)
    resource_retry_delay: timedelta = timedelta(minutes=5)
    correlation_id: str | None = None
    scoped_writer: bool = False

    def __post_init__(self) -> None:
        validate_recovery_runner(self)

    def _egress_organization_owners(self) -> dict[str, str]:
        """Bind every known correlation to its stable operational owner."""
        correlations = set(self.organization_owners or ()) | set(self.resource_organizations or ())
        if self.correlation_id is not None:
            correlations = {self.correlation_id}
        return {correlation: self._organization_for(correlation) for correlation in correlations}

    def _organization_for(self, correlation_id: str) -> str:
        """Resolve durable organizational ownership, never a worker lease identity."""
        configured = (
            None if self.organization_owners is None
            else self.organization_owners.get(correlation_id)
        )
        physical = (
            None if self.resource_organizations is None
            else self.resource_organizations.get(correlation_id)
        )
        if configured is not None and physical is not None and configured != physical:
            raise ValueError("physical and operational organization bindings disagree")
        organization_id = configured if configured is not None else physical
        if not isinstance(organization_id, str) or not organization_id.strip():
            raise ValueError(
                "recovery requires explicit correlation-to-organization ownership"
            )
        return organization_id

    # Looked up through the runner at call time: tests and subclasses replace them.
    _pending_effects = staticmethod(pending_effects)
    _execute_pending = staticmethod(execute_pending)
    _registry_for = staticmethod(registry_for)

    def _run_bounded(
        self, store: Persistence, *, now: datetime, logical_now: datetime | None = None,
    ) -> int:
        service = BoundaryService(store)
        resource_intents = (
            IntentResourceCoordinator(
                self.resource_persistence or self.persistence, self.resource_policies,
                slot_duration=self.resource_slot_duration,
                retry_delay=self.resource_retry_delay,
                owner_epoch=getattr(getattr(store, "lease", None), "epoch", None),
                writer_scope=getattr(getattr(store, "lease", None), "scope", None),
                fencing_persistence=self.persistence,
                effect_scope=(
                    self.persistence.namespace if self.resource_persistence is not None
                    else None
                ),
            )
            if self.resource_policies is not None else None
        )
        return run_bounded_pass(
            self, store, service, resource_intents, now=now, logical_now=logical_now,
        )

    def run_scheduled_trigger(
        self, *, scheduled_for: datetime, observed_at: datetime | None = None,
    ) -> RecoveryTriggerResult:
        """Slot time defines identity; observed time governs physical leases."""
        from sose.jobs.runner import scheduled_trigger_id

        return self.run_trigger(
            trigger_id=scheduled_trigger_id(self.job_id, scheduled_for),
            now=scheduled_for, observed_at=observed_at,
        )

    def run_trigger(
        self, *, trigger_id: str, now: datetime, observed_at: datetime | None = None,
    ) -> RecoveryTriggerResult:
        if not trigger_id:
            raise ValueError("trigger_id must be nonempty")
        config_json = trigger_config_json(self)
        # Enroll all creation paths behind one job-scoped identity fence.
        # Preflight BEFORE any scoped/global writer claim: a failed foreign
        # registration must not advance epochs or bind a correlation.
        with job_registration_guard(self.persistence, self.job_id):
            # PostgreSQL job_state() refreshes the authoritative revision.
            # Do not open an unfenced transaction here: an existing global
            # writer epoch intentionally rejects such transactions.
            prior = self.persistence.job_state(self.job_id)
            check_job_identity(prior, config_json)
            lease = claim_trigger_writer(self, prior, config_json=config_json, now=now)
            store = FencedEnginePersistence(self.persistence, lease)
            with store.transaction() as uow:
                if not activate_trigger(
                    self, uow, trigger_id=trigger_id, config_json=config_json, now=now,
                ):
                    return RecoveryTriggerResult(self.job_id, trigger_id, 0)

        lease_clock = observed_at if observed_at is not None else now
        if self.resource_policies is None:
            # Legacy recovery extension points accept only 'now'. Preserve
            # their call contract and the historical physical lease clock.
            actions = self._run_bounded(store, now=lease_clock)
        else:
            actions = self._run_bounded(store, now=lease_clock, logical_now=now)
        complete_trigger(self, store, trigger_id=trigger_id, now=now)
        return RecoveryTriggerResult(self.job_id, trigger_id, actions)

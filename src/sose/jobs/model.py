from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _require_non_negative(value: int, *, label: str) -> None:
    if value < 0:
        raise ValueError(f"{label} must be >= 0")


def _require_unique_identities(identities: tuple[str, ...], message: str) -> None:
    # Type check first: set() of an unhashable element must not raise TypeError.
    if (
        any(not isinstance(identity, str) or not identity for identity in identities)
        or len(identities) != len(set(identities))
    ):
        raise ValueError(message)


_JOB_STATUSES = frozenset({"ready", "running", "paused", "failed"})
_JOB_PHASES = frozenset({"idle", "advance", "reconcile"})


@dataclass(frozen=True, slots=True)
class CompletedJobTrigger:
    trigger_id: str
    requested_ticks: int
    start_tick: int
    end_tick: int
    config_revision: int
    logical_time: datetime
    run_count: int

    def __post_init__(self) -> None:
        if not self.trigger_id:
            raise ValueError("trigger_id cannot be empty")
        if self.requested_ticks < 1:
            raise ValueError("requested_ticks must be >= 1")
        if self.start_tick < 0 or self.end_tick < self.start_tick:
            raise ValueError("invalid completed trigger tick range")


@dataclass(frozen=True, slots=True)
class OrganizationJobAttestation:
    """Immutable organizational binding of one component job (#496, P496-B).

    Written once, when the job is first registered, and never changed: a runner
    configured with another binding is refused. ``owner_id`` is the Engine owner the
    job writes as, which for a ``SimulationJob`` is its ``job_id``.
    """

    organization_id: str
    definition_fingerprint: str
    config_epoch: int
    component_id: str
    owner_id: str
    domain_name: str

    def __post_init__(self) -> None:
        for label in ("organization_id", "definition_fingerprint", "component_id",
                      "owner_id", "domain_name"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"attestation {label} must be a nonempty canonical identity")
        if (not isinstance(self.config_epoch, int) or isinstance(self.config_epoch, bool)
                or self.config_epoch < 0):
            raise ValueError("attestation config_epoch must be a nonnegative integer")


def require_truthful_attestation(
    attestation: OrganizationJobAttestation | None, job_id: str, domain_name: str,
) -> None:
    """An attested job must write as the attested owner, in the attested domain (#496)."""
    if attestation is None:
        return
    if not isinstance(attestation, OrganizationJobAttestation):
        raise TypeError("organization_attestation requires OrganizationJobAttestation")
    if attestation.owner_id != job_id:
        raise ValueError("a component job writes as its job_id; the attested owner must equal it")
    if attestation.domain_name != domain_name:
        raise ValueError("attested domain contradicts the job's domain definition")


@dataclass(frozen=True, slots=True)
class SimulationJobState:
    """Durable orchestration checkpoint for one recurring simulation job."""

    job_id: str
    domain_name: str
    config_json: str
    config_revision: int
    status: str
    initialized: bool
    logical_time: datetime
    next_tick: int
    run_count: int = 0
    bootstrap_state: object | None = None
    last_triggered_at: datetime | None = None
    active_trigger_id: str | None = None
    last_completed_trigger_id: str | None = None
    active_batch_trigger_id: str | None = None
    active_batch_total_ticks: int = 0
    active_batch_completed_ticks: int = 0
    last_completed_batch_trigger_id: str | None = None
    last_completed_batch_ticks: int = 0
    completed_batch_triggers: tuple[CompletedJobTrigger, ...] = ()
    phase: str = "idle"
    last_error: str | None = None
    # Immutable effect IDs admitted to the currently active recovery trigger.
    # Their business certificates debit this occurrence after process death.
    # Physically mapped effects use authoritative resource-ledger enrollment.
    active_effect_ids: tuple[str, ...] = ()
    # Durable ingress receipts charged to the owning unfinished recurrence.
    # A completed ACK is work even if its domain Command remains pending.
    active_boundary_message_ids: tuple[str, ...] = ()
    # Outbound messages published by this unfinished recurring occurrence.
    # Enrolled atomically with the immutable publication, not afterward.
    active_publication_message_ids: tuple[str, ...] = ()
    # Organizational binding of a component job (#496); None for every other job.
    # The codec omits None, so checkpoints of unbound jobs keep their bytes.
    organization_attestation: OrganizationJobAttestation | None = None
    # next_tick when the active batch was claimed. Recovery derives the batch's
    # start from it: next_tick alone already counts a child tick that advanced and
    # then died. None for checkpoints written before it existed; the codec omits it.
    active_batch_start_tick: int | None = None

    def __post_init__(self) -> None:
        self._validate_identity()
        if self.active_batch_start_tick is not None:
            _require_non_negative(self.active_batch_start_tick, label="active_batch_start_tick")
        if not (self.organization_attestation is None or isinstance(
            self.organization_attestation, OrganizationJobAttestation,
        )):
            raise TypeError("organization_attestation requires OrganizationJobAttestation")
        self._validate_counters()
        _require_unique_identities(
            self.active_effect_ids,
            "active recovery effects require unique nonempty identities",
        )
        _require_unique_identities(
            self.active_boundary_message_ids,
            "active recovery boundary messages require unique nonempty identities",
        )
        _require_unique_identities(
            self.active_publication_message_ids,
            "active recovery publications require unique nonempty identities",
        )
        if self.status not in _JOB_STATUSES:
            raise ValueError(f"unsupported job status: {self.status}")
        if self.phase not in _JOB_PHASES:
            raise ValueError(f"unsupported job phase: {self.phase}")

    def _validate_identity(self) -> None:
        if not self.job_id:
            raise ValueError("job_id cannot be empty")
        if not self.domain_name:
            raise ValueError("domain_name cannot be empty")
        if self.config_revision < 1:
            raise ValueError("config_revision must be >= 1")

    def _validate_counters(self) -> None:
        _require_non_negative(self.next_tick, label="next_tick")
        _require_non_negative(self.run_count, label="run_count")
        _require_non_negative(
            self.active_batch_total_ticks,
            label="active_batch_total_ticks",
        )
        _require_non_negative(
            self.active_batch_completed_ticks,
            label="active_batch_completed_ticks",
        )
        if self.active_batch_completed_ticks > self.active_batch_total_ticks:
            raise ValueError(
                "active_batch_completed_ticks cannot exceed active_batch_total_ticks"
            )
        _require_non_negative(
            self.last_completed_batch_ticks,
            label="last_completed_batch_ticks",
        )

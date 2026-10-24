"""OD-4 bounded durable management work intake: accept, reject, charged sham.

The public persistence writer is NOT an independently protected issuer (#545).
This slice proves committed intervention semantics, not actor reallocation,
resource reservations, completed effects or OD-5b scientific validity (#464).
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from statemachine import State, StateChart

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry, EntityType
from sose.probability import probabilistic_transitions

WORK_KIND = "od4_management_work"
_ADMISSION_EVENT = "od4.work_admitted"
_DECISION_EVENT = "od4.intake_decided"


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ValueError(f"OD-4 {label} requires canonical nonblank identity")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OD-4 intervention time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _guard(store, work_id: str):
    factory = getattr(store, "business_resource_guard", None)
    return factory("od4-intake:" + work_id) if callable(factory) else nullcontext()


@dataclass(frozen=True, slots=True)
class IntakeProtocol:
    model_spec_hash: str
    config_epoch: int
    cost_us: int
    protocol_id: str

    def __post_init__(self) -> None:
        _identity(self.model_spec_hash, "model")
        _identity(self.protocol_id, "protocol")
        if type(self.config_epoch) is not int or self.config_epoch < 0:
            raise ValueError("OD-4 config_epoch must be a nonnegative integer")
        if type(self.cost_us) is not int or self.cost_us <= 0:
            raise ValueError("OD-4 real/sham decision cost must be positive integer")

    def snapshot(self) -> dict:
        return {
            "model_spec_hash": self.model_spec_hash,
            "config_epoch": self.config_epoch,
            "cost_us": self.cost_us,
            "protocol_id": self.protocol_id,
            "version": 1,
        }


@dataclass(slots=True)
class ManagementWork(Entity):
    entity_type: str = WORK_KIND


@probabilistic_transitions({}, excluded_events={"accept", "reject"})
class ManagementIntakeChart(StateChart):
    pending = State(initial=True)
    accepted = State(final=True)
    rejected = State(final=True)

    accept = pending.to(accepted)
    reject = pending.to(rejected)


def _engine(store, *, owner_id: str, at: datetime) -> Engine:
    registry = DomainRegistry()
    registry.register(EntityType(WORK_KIND, ManagementIntakeChart))
    return Engine(
        persistence=store, owner_id=owner_id, registry=registry,
        context=SimulationContext(
            clock=SimulationClock(now=at, step=timedelta(seconds=1)),
            random=RandomSource(root_seed=0), scheduler=Scheduler(),
        ),
    )


def _origin_id(work_id: str) -> str:
    return deterministic_id("od4-work-admission-v1", work_id)


def _check_admission(uow, work: ManagementWork, protocol: IntakeProtocol,
                     owner_id: str, work_key: str) -> None:
    attrs = work.attributes
    if (
        work.entity_type != WORK_KIND or work.id != deterministic_id(
            "od4-work", owner_id, work_key
        )
        or not isinstance(attrs, dict)
        or set(attrs) != {
            "owner_id", "work_key", "protocol", "last_at", "paid_management_us",
            "applied_causes",
        }
        or attrs["owner_id"] != owner_id or attrs["work_key"] != work_key
        or attrs["protocol"] != protocol.snapshot()
        or type(attrs["paid_management_us"]) is not int
        or attrs["paid_management_us"] < 0
        or not isinstance(attrs["applied_causes"], dict)
        or work.state not in ("pending", "accepted", "rejected")
    ):
        raise ValueError("OD-4 work admission/protocol identity has changed")
    origin = tuple(e for e in uow.events() if e.event_id == _origin_id(work.id))
    if (
        len(origin) != 1 or origin[0].name != _ADMISSION_EVENT
        or origin[0].entity_id != work.id or origin[0].owner_id != owner_id
        or origin[0].payload != {
            "owner_id": owner_id, "work_key": work_key,
            "protocol": protocol.snapshot(),
        }
    ):
        raise ValueError("OD-4 work admission lacks original protocol event")


def _verify_decision_ledger(uow, work: ManagementWork,
                            protocol: IntakeProtocol, owner_id: str) -> None:
    """Paid management time must equal the existing durable decision ledger.

    Mutating the projection or replay registry cannot forgive already paid
    interventions. Decision records are the ledger within this public-writer
    reference model; independent signer trust remains a W4b requirement.
    """
    history = tuple(e for e in uow.events() if (
        e.name == _DECISION_EVENT
        and e.entity_type == WORK_KIND and e.entity_id == work.id
    ))
    applied = work.attributes["applied_causes"]
    admission = uow.get_event(_origin_id(work.id))
    # _check_admission already validated this unique original event and its
    # protocol payload. Time starts at the immutable event, not mutable attrs.
    if admission is None or work.created_at != admission.occurred_at:
        raise ValueError("OD-4 work admission timestamp contradicts durable origin")
    last_at = _utc(admission.occurred_at)
    effective_state = "pending"
    cumulative = 0
    seen = set()
    for decision in history:
        payload = decision.payload
        cause = payload.get("cause_id")
        if not isinstance(cause, str) or not cause.strip() or cause in seen:
            raise ValueError("OD-4 durable management cost ledger inconsistent")
        command_id = deterministic_id("od4-command-v1", owner_id, work.id, cause)
        action = payload.get("action")
        observed = _utc(decision.occurred_at)
        next_state = (
            {"accept": "accepted", "reject": "rejected"}.get(action)
            if action != "sham" else effective_state
        )
        # A native transition is a separate immutable causal witness. A
        # decision payload carrying a mere non-null source ID is insufficient:
        # duplicated, deleted or retargeted StateChart evidence must fail
        # closed on subsequent interventions and on idempotent replays.
        sources = tuple(event for event in uow.events() if (
            event.name == "entity.state_transition"
            and event.entity_type == WORK_KIND
            and event.entity_id == work.id
            and event.causation_id == command_id
        ))
        if action == "sham":
            if sources:
                raise ValueError("OD-4 sham has unexpected native transition evidence")
        elif (
            len(sources) != 1
            or sources[0].event_id != payload.get("source_event_id")
            or sources[0].owner_id != owner_id
            or sources[0].occurred_at != decision.occurred_at
            or sources[0].correlation_id != decision.correlation_id
            or sources[0].payload.get("trigger") != action
            or sources[0].payload.get("from_state") != effective_state
            or sources[0].payload.get("to_state") != next_state
        ):
            raise ValueError("OD-4 durable native transition evidence inconsistent")
        cumulative += protocol.cost_us
        if (
            decision.owner_id != owner_id
            or decision.event_id != deterministic_id(
                "od4-intake-v1", owner_id, work.id, cause
            )
            or decision.causation_id != command_id
            or observed < last_at
            or effective_state != "pending"
            or next_state is None
            or payload.get("before") != effective_state
            or payload.get("after") != next_state
            or (
                action == "sham" and (
                    payload.get("matched_action") not in ("accept", "reject")
                    or payload.get("source_event_id") is not None
                )
            )
            or (
                action != "sham" and (
                    payload.get("matched_action") is not None
                    or payload.get("source_event_id") is None
                )
            )
            or payload.get("cost_us") != protocol.cost_us
            or payload.get("paid_management_us") != cumulative
            or payload.get("config_epoch") != protocol.config_epoch
            or payload.get("protocol_id") != protocol.protocol_id
            or payload.get("model_spec_hash") != protocol.model_spec_hash
            or cause not in applied
            or applied[cause] != [
                action, payload.get("matched_action"),
                observed.isoformat(), protocol.snapshot(),
            ]
        ):
            raise ValueError("OD-4 durable management cost ledger inconsistent")
        if action != "sham":
            source_id = payload["source_event_id"]
            sources = tuple(e for e in uow.events() if e.event_id == source_id)
            if (
                len(sources) != 1
                or sources[0].name != "entity.state_transition"
                or sources[0].entity_type != WORK_KIND
                or sources[0].entity_id != work.id
                or sources[0].owner_id != owner_id
                or sources[0].causation_id != command_id
                or sources[0].correlation_id != decision.correlation_id
                or sources[0].occurred_at != decision.occurred_at
                or sources[0].payload.get("trigger") != action
                or sources[0].payload.get("from_state") != effective_state
                or sources[0].payload.get("to_state") != next_state
            ):
                raise ValueError("OD-4 native transition source missing or inconsistent")
        effective_state = next_state
        last_at = observed
        seen.add(cause)
    if (
        len(applied) != len(history)
        or work.attributes["paid_management_us"] != cumulative
        or work.attributes["last_at"] != last_at.isoformat()
        or work.state != effective_state
        or work.updated_at != last_at
        or work.version != len(history)
    ):
        raise ValueError("OD-4 paid management projection contradicts durable ledger")


def admit_work(store, *, owner_id: str, work_key: str,
               protocol: IntakeProtocol, at: datetime) -> str:
    """Freeze a management protocol before observing and charging decisions."""
    _identity(owner_id, "owner")
    _identity(work_key, "work key")
    if not isinstance(protocol, IntakeProtocol):
        raise TypeError("OD-4 admission requires typed protocol")
    now = _utc(at)
    work_id = deterministic_id("od4-work", owner_id, work_key)
    if getattr(store, "transaction_depth", 0):
        raise RuntimeError("OD-4 intake refuses nested transaction")
    with _guard(store, work_id):
        with store.transaction(owner_id=owner_id) as uow:
            if uow.get_entity(WORK_KIND, work_id) is not None:
                raise ValueError("OD-4 work is already admitted")
            uow.save_entity(ManagementWork(
                id=work_id, state="pending", created_at=now, updated_at=now,
                attributes={
                    "owner_id": owner_id, "work_key": work_key,
                    "protocol": protocol.snapshot(), "last_at": now.isoformat(),
                    "paid_management_us": 0, "applied_causes": {},
                },
            ))
            if uow.get_event(_origin_id(work_id)) is not None:
                raise ValueError("OD-4 admission event already exists")
            uow.append_event(DomainEvent(
                event_id=_origin_id(work_id), name=_ADMISSION_EVENT,
                entity_type=WORK_KIND, entity_id=work_id,
                occurred_at=now, owner_id=owner_id,
                correlation_id=deterministic_id("od4-work-flow", owner_id, work_id),
                payload={
                    "owner_id": owner_id, "work_key": work_key,
                    "protocol": protocol.snapshot(),
                },
            ))
    return work_id


def intervene_work(
    store, *, owner_id: str, work_key: str, protocol: IntakeProtocol,
    cause_id: str, action: str, at: datetime,
    matched_action: str | None = None,
) -> DomainEvent:
    """One paid management intervention, committed with Command and native chart.

    Sham charges the same preregistered decision cost but never changes the
    work's structural state. An identical cause is idempotent. All differing
    replays, out-of-order observations and post-terminal interventions fail.
    """
    _identity(owner_id, "owner")
    _identity(work_key, "work key")
    _identity(cause_id, "cause")
    if not isinstance(protocol, IntakeProtocol):
        raise TypeError("OD-4 intervention requires typed protocol")
    if action not in ("accept", "reject", "sham"):
        raise ValueError("OD-4 unsupported management intervention")
    if (action == "sham") != (matched_action in ("accept", "reject")):
        raise ValueError("OD-4 sham requires a real matched accept/reject action")
    if action != "sham" and matched_action is not None:
        raise ValueError("OD-4 real intervention cannot declare a sham pairing")
    now = _utc(at)
    work_id = deterministic_id("od4-work", owner_id, work_key)
    command_id = deterministic_id("od4-command-v1", owner_id, work_id, cause_id)
    decision_id = deterministic_id("od4-intake-v1", owner_id, work_id, cause_id)
    request = (
        action, matched_action, now.isoformat(), protocol.snapshot(),
    )
    if getattr(store, "transaction_depth", 0):
        raise RuntimeError("OD-4 intervention refuses nested transaction")
    with _guard(store, work_id):
        with store.transaction(owner_id=owner_id) as uow:
            work = uow.get_entity(WORK_KIND, work_id)
            if work is None:
                raise KeyError(f"OD-4 work not admitted: {work_id}")
            _check_admission(uow, work, protocol, owner_id, work_key)
            _verify_decision_ledger(uow, work, protocol, owner_id)
            recorded = work.attributes["applied_causes"].get(cause_id)
            original = uow.get_event(decision_id)
            if recorded is not None:
                if recorded != list(request):
                    raise ValueError("OD-4 cause conflicts with committed request")
                same = tuple(e for e in uow.events() if e.event_id == decision_id)
                if len(same) != 1 or same[0] != original or original is None:
                    raise ValueError("OD-4 replay lacks unique causal decision")
                command = uow.get_command(command_id)
                if (
                    command is None
                    or command.command_id != command_id
                    or command.name != action
                    or command.entity_type != WORK_KIND
                    or command.entity_id != work_id
                    or command.owner_id != owner_id
                    or command.due_at != now or command.issued_at != now
                    or command.causation_id is not None
                    or command.correlation_id != deterministic_id(
                        "od4-work-flow", owner_id, work_id
                    )
                    or command.payload.get("request") != list(request)
                    or command.payload.get("cause_id") != cause_id
                    or command.payload.get("protocol") != protocol.snapshot()
                    or original.causation_id != command_id
                    or original.payload.get("action") != action
                    or original.payload.get("matched_action") != matched_action
                    or original.payload.get("cost_us") != protocol.cost_us
                    or original.payload.get("config_epoch") != protocol.config_epoch
                    or original.owner_id != owner_id
                    or original.entity_id != work_id
                    or original.occurred_at != now
                ):
                    raise ValueError("OD-4 replay lacks corresponding command")
                return original
            if original is not None or uow.get_command(command_id) is not None:
                raise ValueError("OD-4 cause identity already used")
            if work.state != "pending":
                raise ValueError("OD-4 cannot manage terminal work")
            if work.created_at is None or now < _utc(work.created_at):
                raise ValueError("OD-4 intervention predates admission")
            previous_at = _utc(datetime.fromisoformat(work.attributes["last_at"]))
            if now < previous_at:
                raise ValueError("OD-4 intervention time regressed")
            before = work.state
            command = Command(
                command_id=command_id, name=action,
                entity_type=WORK_KIND, entity_id=work_id,
                due_at=now, issued_at=now, owner_id=owner_id,
                correlation_id=deterministic_id("od4-work-flow", owner_id, work_id),
                payload={
                    "request": list(request), "cause_id": cause_id,
                    "protocol": protocol.snapshot(),
                },
            )
            source_event_id = None
            if action != "sham":
                runtime = _engine(store, owner_id=owner_id, at=now)
                try:
                    chart = runtime.context.statecharts.bind(work, caused_by=command)
                    chart.send(action)
                    emitted = runtime.context.drain_events()
                    if len(emitted) != 1:
                        raise RuntimeError("OD-4 intervention requires one native transition")
                    source = emitted[0]
                    source_event_id = source.event_id
                    if source.name != "entity.state_transition":
                        raise RuntimeError("OD-4 transition lacks canonical source event")
                    uow.append_event(source)
                finally:
                    runtime.context.drain_events()
            else:
                work.touch(now)
            work.attributes["last_at"] = now.isoformat()
            work.attributes["paid_management_us"] += protocol.cost_us
            work.attributes["applied_causes"][cause_id] = list(request)
            effect = DomainEvent(
                event_id=decision_id, name=_DECISION_EVENT,
                entity_type=WORK_KIND, entity_id=work_id,
                occurred_at=now, owner_id=owner_id,
                causation_id=command_id,
                correlation_id=command.correlation_id,
                payload={
                    "cause_id": cause_id, "action": action,
                    "matched_action": matched_action, "before": before,
                    "after": work.state, "cost_us": protocol.cost_us,
                    "paid_management_us": work.attributes["paid_management_us"],
                    "config_epoch": protocol.config_epoch,
                    "protocol_id": protocol.protocol_id,
                    "model_spec_hash": protocol.model_spec_hash,
                    "source_event_id": source_event_id,
                    "work_version": work.version,
                },
            )
            uow.save_entity(work)
            uow.save_command(command)
            uow.append_event(effect)
            return effect

"""OD-2 bounded durable actor lifecycle on existing SOSE Entity/StateChart/UoW.

This does not allocate a technician/physical ResourceReservation or establish
actor availability for OD-5b. It accounts for *observed synthetic* actor-time
and paid transitions with stable owner, model, epoch and causal command IDs.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
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
from sose.persistence.base import Persistence
from sose.probability import probabilistic_transitions


ACTOR_KIND = "organizational_actor"
ACTOR_STATES = (
    "admitted", "eligible", "active", "unavailable", "development", "exited",
)


@dataclass(slots=True)
class OrganizationalActor(Entity):
    entity_type: str = ACTOR_KIND


@probabilistic_transitions(
    {},
    excluded_events={
        "activate", "assign", "complete", "make_unavailable",
        "resume", "begin_development", "finish_development", "exit",
    },
)
class OrganizationalActorChart(StateChart):
    admitted = State(initial=True)
    eligible = State()
    active = State()
    unavailable = State()
    development = State()
    exited = State(final=True)

    activate = admitted.to(eligible)
    assign = eligible.to(active)
    complete = active.to(eligible)
    make_unavailable = eligible.to(unavailable)
    resume = unavailable.to(eligible)
    begin_development = eligible.to(development)
    finish_development = development.to(eligible)
    exit = (
        eligible.to(exited)
        | unavailable.to(exited)
        | development.to(exited)
    )


def _utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("actor effective time requires timezone-aware datetime")
    return at.astimezone(timezone.utc)



@dataclass(frozen=True, slots=True)
class ActorAvailabilityInterval:
    """An explicit, half-open UTC eligibility interval, not physical ownership."""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        start, end = _utc(self.starts_at), _utc(self.ends_at)
        if start >= end:
            raise ValueError("actor calendar interval requires positive duration")
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)


@dataclass(frozen=True, slots=True)
class ActorAvailabilityCalendar:
    """Immutable bounded schedule pinned to the actor's original config epoch."""

    version: int
    config_epoch: int
    intervals: tuple[ActorAvailabilityInterval, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("actor calendar requires supported positive version 1")
        if type(self.config_epoch) is not int or self.config_epoch < 0:
            raise ValueError("actor calendar config epoch must be nonnegative")
        if not isinstance(self.intervals, tuple) or not self.intervals:
            raise ValueError("actor calendar requires immutable nonempty intervals")
        if any(not isinstance(item, ActorAvailabilityInterval) for item in self.intervals):
            raise TypeError("actor calendar intervals must be typed immutable instances")
        ordered = tuple(sorted(self.intervals, key=lambda x: (x.starts_at, x.ends_at)))
        if self.intervals != ordered:
            raise ValueError("actor calendar intervals must be canonically ordered")
        if any(a.ends_at > b.starts_at for a, b in zip(ordered, ordered[1:])):
            raise ValueError("actor calendar intervals overlap")

    def snapshot(self) -> dict:
        return {
            "version": self.version,
            "config_epoch": self.config_epoch,
            "intervals": [
                {"start": item.starts_at.isoformat(), "end": item.ends_at.isoformat()}
                for item in self.intervals
            ],
        }

    def digest(self) -> str:
        return sha256(dumps(
            self.snapshot(), sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()


def _calendar_origin_event_id(actor_id: str) -> str:
    """Durable provenance key based only on immutable original actor identity.

    The owner is bound in the immutable event payload and actor identity, not
    used to *find* the marker: actor.attributes.owner_id is mutable projection.
    """
    return deterministic_id("od2-actor-calendar-admitted", actor_id)


def _require_actor_calendar_start(
    uow, actor: OrganizationalActor, at: datetime, *, require_window: bool,
) -> None:
    """Verify enrolled source against independent persisted admission event.

    The canonical immutable event is committed with actor enrollment; its
    digest and full source are checked against the mutable actor projection.
    A missing projection is *not* interpreted as an opt-out if a calendar was
    originally enrolled. Same-cause replay validates before returning.
    """
    attributes = actor.attributes
    saved = attributes.get("availability_calendar")
    origin = uow.get_event(_calendar_origin_event_id(actor.id))
    if origin is None and saved is None:
        # Actors enrolled before opt-in calendar support stay calendar-free.
        return
    if origin is None or saved is None:
        raise ValueError("actor calendar original provenance missing")
    try:
        if (
            origin.name != "od2.actor_calendar_admitted"
            or origin.owner_id != attributes["owner_id"]
            or origin.entity_type != ACTOR_KIND
            or origin.entity_id != actor.id
            or not isinstance(saved, dict)
            or set(saved) != {"version", "config_epoch", "intervals", "source_digest"}
            or not isinstance(saved["intervals"], list)
        ):
            raise ValueError("actor calendar source or durable marker malformed")
        windows = []
        for item in saved["intervals"]:
            if not isinstance(item, dict) or set(item) != {"start", "end"}:
                raise ValueError("malformed actor calendar interval")
            windows.append(ActorAvailabilityInterval(
                datetime.fromisoformat(item["start"]),
                datetime.fromisoformat(item["end"]),
            ))
        calendar = ActorAvailabilityCalendar(
            version=saved["version"],
            config_epoch=saved["config_epoch"],
            intervals=tuple(windows),
        )
        if (
            calendar.config_epoch != attributes["config_epoch"]
            or saved["source_digest"] != calendar.digest()
            or origin.payload.get("calendar_digest") != calendar.digest()
            or origin.payload.get("calendar_source") != calendar.snapshot()
            or origin.payload.get("model_spec_hash") != attributes["model_spec_hash"]
            or origin.payload.get("config_epoch") != attributes["config_epoch"]
        ):
            raise ValueError("actor calendar digest, epoch or immutable source mismatched")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("actor calendar digest/shape contradicts authoritative enrollment") from exc
    if require_window and not any(
        window.starts_at <= at < window.ends_at for window in calendar.intervals
    ):
        raise ValueError("actor calendar excludes assignment start")


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{label} must be nonempty")


def _guard(store: Persistence, actor_id: str):
    factory = getattr(store, "business_resource_guard", None)
    return (
        factory("od2-actor:" + actor_id)
        if callable(factory) else nullcontext()
    )


def _engine(store: Persistence, *, owner_id: str, at: datetime) -> Engine:
    context = SimulationContext(
        clock=SimulationClock(now=at, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=0),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType(ACTOR_KIND, OrganizationalActorChart))
    return Engine(
        persistence=store, owner_id=owner_id, registry=registry,
        context=context,
    )


def seed_actor(
    store: Persistence, *, owner_id: str, actor_key: str,
    model_spec_hash: str, config_epoch: int, at: datetime,
    replaces_actor_id: str | None = None,
    availability_calendar: ActorAvailabilityCalendar | None = None,
    tracked_time: bool = False,
) -> str:
    """Enroll an actor; optional v1 original-time evidence is prospective only."""
    _identity(owner_id, "owner_id")
    _identity(actor_key, "actor_key")
    _identity(model_spec_hash, "model_spec_hash")
    if type(config_epoch) is not int or config_epoch < 0:
        raise ValueError("config_epoch must be a nonnegative integer")
    now = _utc(at)
    if type(tracked_time) is not bool:
        raise TypeError("tracked_time must be an explicit boolean")
    if availability_calendar is not None:
        if not isinstance(availability_calendar, ActorAvailabilityCalendar):
            raise TypeError("availability_calendar must be ActorAvailabilityCalendar")
        if availability_calendar.config_epoch != config_epoch:
            raise ValueError("actor calendar epoch contradicts admitted actor epoch")
    identity = deterministic_id("od2-actor", owner_id, actor_key)
    if replaces_actor_id is not None:
        _identity(replaces_actor_id, "replaces_actor_id")
        if replaces_actor_id == identity:
            raise ValueError("actor cannot replace itself")
    # A nested Memory snapshot can erase an independently committed actor or
    # replacement claim; SQLite refuses nesting, so reject consistently.
    if getattr(store, "transaction_depth", 0):
        raise RuntimeError("OD-2 actor admission refuses nested transaction")
    # A replacement is serialized by the PREDECESSOR resource identity, not
    # by a new actor ID. Otherwise two concurrently proposed successors
    # could both pass a check on the old actor and fork its lineage.
    with _guard(store, replaces_actor_id or identity):
        with store.transaction(owner_id=owner_id) as uow:
            prior = uow.get_entity(ACTOR_KIND, identity)
            if prior is not None:
                raise ValueError("actor identity already exists")
            if replaces_actor_id is not None:
                replaced = uow.get_entity(ACTOR_KIND, replaces_actor_id)
                if (
                    replaced is None or replaced.state != "exited"
                    or replaced.attributes["owner_id"] != owner_id
                    or now < datetime.fromisoformat(replaced.attributes["last_at"])
                ):
                    raise ValueError("replacement requires prior exited actor in same owner")
                if replaced.attributes.get("replaced_by_actor_id") is not None:
                    raise ValueError("predecessor already replaced")
                if (
                    replaced.attributes["config_epoch"] != config_epoch
                    or replaced.attributes["model_spec_hash"] != model_spec_hash
                ):
                    # No live admitted migration/config epoch protocol exists
                    # in this bounded slice. Refuse silent model lineage drift
                    # rather than treating arbitrary successor input as an
                    # authoritative configuration update.
                    raise ValueError("replacement model/epoch requires separate admission")
                replaced.attributes["replaced_by_actor_id"] = identity
                replaced.touch(now)
                uow.save_entity(replaced)
            uow.save_entity(OrganizationalActor(
                id=identity, state="admitted", created_at=now, updated_at=now,
                attributes={
                    "owner_id": owner_id, "model_spec_hash": model_spec_hash,
                    "config_epoch": config_epoch, "last_at": now.isoformat(),
                    "time_us": {state: 0 for state in ACTOR_STATES},
                    "paid_transition_us": 0,
                    "assigned_work_id": None, "applied_causes": {},
                    "replaces_actor_id": replaces_actor_id,
                    "replaced_by_actor_id": None,
                    **({"tracked_time_version": 1} if tracked_time else {}),
                    **({
                        "availability_calendar": {
                            **availability_calendar.snapshot(),
                            "source_digest": availability_calendar.digest(),
                        },
                    } if availability_calendar is not None else {}),
                },
            ))
            if availability_calendar is not None:
                # An independent append-only record is the enrollment source
                # of truth. The entity's source_digest is only a projection;
                # changing or deleting it cannot silently remove eligibility.
                uow.append_event(DomainEvent(
                    event_id=_calendar_origin_event_id(identity),
                    name="od2.actor_calendar_admitted",
                    entity_type=ACTOR_KIND, entity_id=identity,
                    occurred_at=now, owner_id=owner_id,
                    correlation_id=deterministic_id("od2-actor-flow", owner_id, identity),
                    payload={
                        "calendar_source": availability_calendar.snapshot(),
                        "calendar_digest": availability_calendar.digest(),
                        "config_epoch": config_epoch,
                        "model_spec_hash": model_spec_hash,
                    },
                ))
            if tracked_time:
                uow.append_event(DomainEvent(
                    event_id=_actor_time_origin_event_id(identity),
                    name="od2.actor_time_origin_recorded",
                    entity_type=ACTOR_KIND, entity_id=identity,
                    owner_id=owner_id, occurred_at=now,
                    correlation_id=deterministic_id("od2-actor-flow", owner_id, identity),
                    payload={
                        "owner_id": owner_id,
                        "model_spec_hash": model_spec_hash,
                        "config_epoch": config_epoch,
                        "replaces_actor_id": replaces_actor_id,
                        "tracked_time_version": 1,
                    },
                ))
    return identity


def _actor_time_origin_event_id(actor_id: str) -> str:
    return deterministic_id("od2-actor-time-origin-v1", actor_id)


def _committed_work_event(
    *, command: Command, actor_id: str, cause_id: str,
    source: DomainEvent, model_spec_hash: str, config_epoch: int,
    work_id: str | None, cost_us: int,
) -> DomainEvent:
    """Append-only work/cost evidence causally attached to a native event.

    The event is deliberately NOT a native entity.state_transition. It binds
    work identity to the original StateChart event without turning mutable
    Commands or projection attributes into historical authorization.
    """
    return DomainEvent(
        event_id=deterministic_id(
            "od2-actor-work-evidence-v1", command.owner_id, actor_id, cause_id,
        ),
        name="od2.actor_work_caused",
        entity_type="organizational_actor_work_cause",
        entity_id=command.command_id,
        occurred_at=source.occurred_at,
        owner_id=command.owner_id,
        causation_id=source.event_id,
        correlation_id=command.correlation_id,
        payload={
            "actor_id": actor_id, "command_id": command.command_id,
            "source_event_id": source.event_id, "action": command.name,
            "external_cause_id": cause_id, "work_id": work_id,
            "cost_us": cost_us, "model_spec_hash": model_spec_hash,
            "config_epoch": config_epoch,
        },
    )




def _committed_nonwork_cost_event(
    *, command: Command, actor_id: str, cause_id: str,
    source: DomainEvent, model_spec_hash: str, config_epoch: int,
    cost_us: int,
) -> DomainEvent:
    """Versioned immutable non-work cost ledger entry in the source UoW."""
    return DomainEvent(
        event_id=deterministic_id(
            "od2-nonwork-cost-v1", command.owner_id, actor_id, cause_id,
        ),
        name="od2.actor_nonwork_cost_caused",
        entity_type="organizational_actor_paid_transition",
        entity_id=command.command_id, owner_id=command.owner_id,
        occurred_at=source.occurred_at,
        causation_id=source.event_id, correlation_id=command.correlation_id,
        payload={
            "actor_id": actor_id, "command_id": command.command_id,
            "source_event_id": source.event_id, "action": command.name,
            "external_cause_id": cause_id,
            "cost_us": cost_us, "model_spec_hash": model_spec_hash,
            "config_epoch": config_epoch,
        },
    )


def _verify_actor_time_projection(uow, actor: OrganizationalActor) -> None:
    """Reconstruct bounded actor-time from original committed Command/events.

    This is a consistency/fail-closed control, NOT an independent source
    authorization or physical-capacity grant. Legacy unverifiable histories
    must be migrated through an independently authorized cutover, not guessed
    from mutable attributes. No persisted record is modified by this audit.
    """
    meta = actor.attributes
    if not isinstance(meta, dict) or not isinstance(meta.get("applied_causes"), dict):
        raise ValueError("OD-2 actor-time ledger lacks durable cause index")
    events = tuple(uow.events())
    # One snapshot/index per guard; avoid repeated shared-ledger scans for each
    # historical actor cost receipt inside an owner-serialized transaction.
    by_id = {}
    for event in events:
        by_id.setdefault(event.event_id, []).append(event)
    source_events = tuple(event for event in events if (
        event.entity_type == ACTOR_KIND and event.entity_id == actor.id
        and event.name == "entity.state_transition"
    ))
    if len(source_events) != len(meta["applied_causes"]):
        raise ValueError("OD-2 actor-time causal history incomplete or duplicated")
    amounts = {state: 0 for state in ACTOR_STATES}
    paid_us = 0
    current_state = "admitted"
    origins = by_id.get(_actor_time_origin_event_id(actor.id), ())
    origin = origins[0] if len(origins) == 1 else None
    if (
        origin is None
        or origin.name != "od2.actor_time_origin_recorded"
        or origin.entity_type != ACTOR_KIND or origin.entity_id != actor.id
        or origin.owner_id != meta.get("owner_id")
        or origin.payload != {
            "owner_id": meta.get("owner_id"),
            "model_spec_hash": meta.get("model_spec_hash"),
            "config_epoch": meta.get("config_epoch"),
            "replaces_actor_id": meta.get("replaces_actor_id"),
            "tracked_time_version": 1,
        }
        or meta.get("tracked_time_version") != 1
        or actor.created_at != origin.occurred_at
    ):
        raise ValueError("OD-2 actor-time enrollment source contradicts projection")
    last_at = _utc(origin.occurred_at)
    assigned_work = None
    seen_causes = set()
    for source in source_events:
        command = uow.get_command(source.causation_id)
        if command is None or not isinstance(command.payload, dict):
            raise ValueError("OD-2 actor-time missing original causal Command")
        payload = command.payload
        cause = payload.get("external_cause_id")
        work_id = payload.get("work_id")
        cost = payload.get("cost_us")
        event_at = _utc(source.occurred_at)
        if (
            not isinstance(cause, str) or not cause.strip() or cause in seen_causes
            or command.command_id != deterministic_id(
                "od2-actor-command", source.owner_id, actor.id, cause
            )
            or command.owner_id != meta.get("owner_id")
            or source.owner_id != command.owner_id
            or command.entity_type != ACTOR_KIND or command.entity_id != actor.id
            or command.name != source.payload.get("trigger")
            or command.due_at != event_at or command.issued_at != event_at
            or source.correlation_id != command.correlation_id
            or payload.get("evidence_version") != 1
            or payload.get("model_spec_hash") != meta.get("model_spec_hash")
            or payload.get("config_epoch") != meta.get("config_epoch")
            or type(cost) is not int or cost < 0
            or source.payload.get("from_state") != current_state
            or source.payload.get("to_state") not in ACTOR_STATES
            or event_at < last_at
        ):
            raise ValueError("OD-2 actor-time causal lineage contradicts original history")
        action = command.name
        if action == "assign":
            if assigned_work is not None or not isinstance(work_id, str) or not work_id.strip():
                raise ValueError("OD-2 actor-time duplicate or unidentifiable assignment")
            assigned_work = work_id
        elif action == "complete":
            if assigned_work is None or assigned_work != work_id:
                raise ValueError("OD-2 actor-time completion does not match assignment")
            assigned_work = None
        elif work_id is not None:
            raise ValueError("OD-2 actor-time work ID on non-work transition")
        if action in ("assign", "complete"):
            expected = _committed_work_event(
                command=command, actor_id=actor.id, cause_id=cause,
                source=source, model_spec_hash=meta["model_spec_hash"],
                config_epoch=meta["config_epoch"], work_id=work_id,
                cost_us=cost,
            )
        else:
            expected = _committed_nonwork_cost_event(
                command=command, actor_id=actor.id, cause_id=cause,
                source=source, model_spec_hash=meta["model_spec_hash"],
                config_epoch=meta["config_epoch"], cost_us=cost,
            )
        evidence = by_id.get(expected.event_id, ())
        if len(evidence) != 1 or evidence[0] != expected:
            raise ValueError("OD-2 actor-time original work/nonwork cost evidence missing or modified")
        expected_cause = [
            action, event_at.isoformat(), work_id, cost,
        ]
        if meta["applied_causes"].get(cause) != expected_cause:
            raise ValueError("OD-2 actor-time projection contradicts causal history")
        amounts[current_state] += (event_at - last_at) // timedelta(microseconds=1)
        paid_us += cost
        last_at = event_at
        current_state = source.payload["to_state"]
        seen_causes.add(cause)
    if (
        set(meta["applied_causes"]) != seen_causes
        or meta.get("time_us") != amounts
        or type(meta.get("paid_transition_us")) is not int
        or meta["paid_transition_us"] != paid_us
        or meta.get("last_at") != last_at.isoformat()
        or actor.state != current_state
        or meta.get("assigned_work_id") != assigned_work
    ):
        raise ValueError("OD-2 actor-time projection contradicts committed time/cost ledger")


def transition_actor(
    store: Persistence, *, owner_id: str, actor_id: str,
    action: str, cause_id: str, at: datetime,
    model_spec_hash: str, config_epoch: int,
    work_id: str | None = None, cost_us: int = 0,
) -> OrganizationalActor:
    """Atomically commit lifecycle, actor-time, paid cost, Command and event.

    Duplicate *identical* causes return the prior durable state unchanged.
    A future/stale config epoch, opposite owner, illegal StateChart transition
    or in-flight exit cannot mutate any of the five records.
    """
    for val, label in (
        (owner_id, "owner_id"), (actor_id, "actor_id"),
        (action, "action"), (cause_id, "cause_id"),
        (model_spec_hash, "model_spec_hash"),
    ):
        _identity(val, label)
    if type(config_epoch) is not int or config_epoch < 0:
        raise ValueError("config_epoch must be a nonnegative integer")
    if type(cost_us) is not int or cost_us < 0:
        raise ValueError("paid transition cost must be nonnegative microseconds")
    now = _utc(at)
    identity = (action, now.isoformat(), work_id, cost_us)
    # A work assignment and its paid cost cannot be established solely by
    # mutable actor.attributes. Persist the canonical identity on the Command
    # that actually caused the chart event.
    command = Command(
        command_id=deterministic_id("od2-actor-command", owner_id, actor_id, cause_id),
        name=action, entity_type=ACTOR_KIND, entity_id=actor_id,
        due_at=now, issued_at=now, owner_id=owner_id,
        causation_id=None,
        correlation_id=deterministic_id("od2-actor-flow", owner_id, actor_id),
        payload={
            "evidence_version": 1, "external_cause_id": cause_id,
            "work_id": work_id, "cost_us": cost_us,
            "model_spec_hash": model_spec_hash, "config_epoch": config_epoch,
        },
    )
    # Never commit a lifecycle Command/event inside an independently owned UoW.
    # The parent's final snapshot would otherwise erase or orphan that evidence.
    if getattr(store, "transaction_depth", 0):
        raise RuntimeError("OD-2 actor transition refuses nested transaction")
    with _guard(store, actor_id):
        with store.transaction(owner_id=owner_id) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            if actor is None:
                raise KeyError(f"unknown organizational actor: {actor_id}")
            meta = actor.attributes
            if meta["owner_id"] != owner_id:
                raise PermissionError("actor belongs to another operational owner")
            if meta["model_spec_hash"] != model_spec_hash:
                raise ValueError("actor model_spec_hash changed")
            if type(meta.get("config_epoch")) is not int or meta["config_epoch"] != config_epoch:
                raise ValueError("stale or unadmitted actor config_epoch")
            _require_actor_calendar_start(
                uow, actor, now, require_window=False,
            )
            used = meta["applied_causes"].get(cause_id)
            if used is not None:
                if used != list(identity):
                    raise ValueError("actor cause was replayed with different action")
                persisted_command = uow.get_command(command.command_id)
                if persisted_command != command:
                    raise ValueError(
                        "actor replay lacks canonical durable Command/work/cost identity"
                    )
                matching = tuple(event for event in uow.events() if (
                    event.causation_id == command.command_id
                    and event.entity_type == ACTOR_KIND
                    and event.entity_id == actor_id
                    and event.owner_id == owner_id
                ))
                if (
                    len(matching) != 1
                    or matching[0].name != "entity.state_transition"
                    or matching[0].payload.get("trigger") != action
                    or matching[0].occurred_at != now
                    or matching[0].correlation_id != command.correlation_id
                ):
                    raise ValueError(
                        "actor replay lacks matching durable StateChart source event"
                    )
                if action in ("assign", "complete"):
                    expected = _committed_work_event(
                        command=command, actor_id=actor_id, cause_id=cause_id,
                        source=matching[0], model_spec_hash=model_spec_hash,
                        config_epoch=config_epoch, work_id=work_id, cost_us=cost_us,
                    )
                    matches = tuple(
                        event for event in uow.events() if event.event_id == expected.event_id
                    )
                    if len(matches) != 1 or matches[0] != expected:
                        raise ValueError(
                            "actor replay lacks immutable work/cost causal evidence"
                        )
                return actor
            # Preserve the stronger original replay-specific identity checks.
            # Before any NEW native transition, refuse to launder mutable
            # actor-time buckets or paid costs as independently reached state.
            enrolled_time = uow.get_event(_actor_time_origin_event_id(actor_id))
            if meta.get("tracked_time_version") is not None or enrolled_time is not None:
                _verify_actor_time_projection(uow, actor)
            prior_at = datetime.fromisoformat(meta["last_at"])
            if now < prior_at:
                raise ValueError("actor effective time regressed")
            if action == "assign":
                _require_actor_calendar_start(uow, actor, now, require_window=True)
                _identity(work_id, "work_id")
                if meta["assigned_work_id"] is not None:
                    raise ValueError("actor already assigned")
            elif action == "complete":
                if work_id is None or work_id != meta["assigned_work_id"]:
                    raise ValueError("completion requires the matching assigned work")
            elif work_id is not None:
                raise ValueError("work identity is only legal for assign/complete")
            if action == "begin_development" and cost_us == 0:
                raise ValueError("development must have explicit paid cost")

            engine = _engine(store, owner_id=owner_id, at=now)
            try:
                chart = engine.context.statecharts.bind(actor, caused_by=command)
                chart.send(action)
                emitted = engine.context.drain_events()
                if len(emitted) != 1:
                    raise RuntimeError("actor transition must produce exactly one causal event")
                elapsed_us = (now - prior_at) // timedelta(microseconds=1)
                meta["time_us"][emitted[0].payload["from_state"]] += elapsed_us
                meta["last_at"] = now.isoformat()
                meta["paid_transition_us"] += cost_us
                meta["applied_causes"][cause_id] = list(identity)
                if action == "assign":
                    meta["assigned_work_id"] = work_id
                if action == "complete":
                    meta["assigned_work_id"] = None
                uow.save_entity(actor)
                uow.save_command(command)
                for event in emitted:
                    uow.append_event(event)
                if action in ("assign", "complete"):
                    work_evidence = _committed_work_event(
                        command=command, actor_id=actor_id, cause_id=cause_id,
                        source=emitted[0], model_spec_hash=model_spec_hash,
                        config_epoch=config_epoch, work_id=work_id, cost_us=cost_us,
                    )
                    if uow.get_event(work_evidence.event_id) is not None:
                        raise ValueError("actor work evidence ID already committed")
                    uow.append_event(work_evidence)
                elif meta.get("tracked_time_version") == 1:
                    # Paid and zero-cost non-work transitions both require an
                    # immutable counterpart, preventing cost=0 downgrades.
                    cost_evidence = _committed_nonwork_cost_event(
                        command=command, actor_id=actor_id, cause_id=cause_id,
                        source=emitted[0], model_spec_hash=model_spec_hash,
                        config_epoch=config_epoch, cost_us=cost_us,
                    )
                    if uow.get_event(cost_evidence.event_id) is not None:
                        raise ValueError("actor cost evidence ID already committed")
                    uow.append_event(cost_evidence)
                return actor
            finally:
                engine.context.drain_events()

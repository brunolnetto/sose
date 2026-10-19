"""OD-2 bounded durable actor lifecycle on existing SOSE Entity/StateChart/UoW.

This does not allocate a technician/physical ResourceReservation or establish
actor availability for OD-5b. It accounts for *observed synthetic* actor-time
and paid transitions with stable owner, model, epoch and causal command IDs.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
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
        "cancel_assigned",
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
    cancel_assigned = active.to(eligible)
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
    if origin is None:
        raise ValueError("actor immutable admission evidence missing")
    if (
        origin.entity_type != ACTOR_KIND
        or origin.entity_id != actor.id
        or origin.owner_id != attributes.get("owner_id")
        or origin.occurred_at != actor.created_at
        or origin.causation_id is not None
        or origin.correlation_id != deterministic_id(
            "od2-actor-flow", origin.owner_id, actor.id,
        )
        or origin.payload.get("model_spec_hash") != attributes.get("model_spec_hash")
        or type(origin.payload.get("config_epoch")) is not int
        or origin.payload["config_epoch"] != attributes.get("config_epoch")
        or origin.payload.get("admitted_at") != actor.created_at.isoformat()
        or origin.payload.get("replaces_actor_id") != attributes.get("replaces_actor_id")
    ):
        raise ValueError("actor immutable admission provenance contradicts projection")
    if saved is None:
        if (
            origin.name != "od2.actor_admitted"
            or "calendar_source" in origin.payload
            or "calendar_digest" in origin.payload
        ):
            raise ValueError("actor calendar original provenance missing")
        return
    if origin.name != "od2.actor_calendar_admitted":
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
) -> str:
    """Enroll an independent actor; optional replacement requires exited peer."""
    _identity(owner_id, "owner_id")
    _identity(actor_key, "actor_key")
    _identity(model_spec_hash, "model_spec_hash")
    if type(config_epoch) is not int or config_epoch < 0:
        raise ValueError("config_epoch must be a nonnegative integer")
    now = _utc(at)
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
                _require_actor_calendar_start(
                    uow, replaced, now, require_window=False,
                )
                open_work, attested_state, cancelled = _attested_actor_replay(
                    uow, actor_id=replaces_actor_id, owner_id=owner_id,
                )
                if (
                    attested_state != "exited"
                    or replaced.state != attested_state
                    or open_work is not None
                    or replaced.attributes.get("assigned_work_id") is not None
                    or replaced.attributes.get("cancelled_work_ids") != cancelled
                ):
                    raise ValueError("replacement predecessor contradicts immutable lifecycle")
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
                    "cancelled_work_ids": [],
                    "replaces_actor_id": replaces_actor_id,
                    "replaced_by_actor_id": None,
                    **({
                        "availability_calendar": {
                            **availability_calendar.snapshot(),
                            "source_digest": availability_calendar.digest(),
                        },
                    } if availability_calendar is not None else {}),
                },
            ))
            # Immutable enrollment is mandatory even without a calendar.
            # Mutable actor attributes can never invent owner or config lineage.
            uow.append_event(DomainEvent(
                event_id=_calendar_origin_event_id(identity),
                name=("od2.actor_calendar_admitted" if availability_calendar is not None
                      else "od2.actor_admitted"),
                entity_type=ACTOR_KIND, entity_id=identity,
                occurred_at=now, owner_id=owner_id,
                correlation_id=deterministic_id("od2-actor-flow", owner_id, identity),
                payload={
                    "admitted_at": now.isoformat(),
                    "replaces_actor_id": replaces_actor_id,
                    "config_epoch": config_epoch,
                    "model_spec_hash": model_spec_hash,
                    **({
                        "calendar_source": availability_calendar.snapshot(),
                        "calendar_digest": availability_calendar.digest(),
                    } if availability_calendar is not None else {}),
                },
            ))
    return identity


def _verify_actor_work_command(
    uow, *, actor_id: str, owner_id: str,
    event: DomainEvent, action: str,
    descendant_counts: dict[str, int] | None = None,
) -> None:
    """Authenticate one assign/complete against its exact issued Command.

    Old in-flight v1 commands/events omitted work IDs on both sides. New
    assignments and completions bind the identity in *both* durable records.
    A Command for activate/another actor/owner/cause cannot be recycled as
    evidence of work. No inferred global clock or coordination is needed.
    """
    if not isinstance(event.causation_id, str) or not event.causation_id:
        raise ValueError("attested work transition lacks causal Command")
    command = uow.get_command(event.causation_id)
    if command is None:
        raise ValueError("attested work transition causal Command missing")
    cause = command.payload.get("external_cause_id")
    if not isinstance(cause, str) or not cause.strip():
        raise ValueError("attested work transition original cause missing")
    payload = {"external_cause_id": cause}
    key = "assigned_work_id" if action == "assign" else "completed_work_id"
    event_work = event.payload.get(key)
    command_work = command.payload.get(key)
    if event_work is not None or command_work is not None:
        if (
            not isinstance(event_work, str) or not event_work.strip()
            or command_work != event_work
        ):
            raise ValueError("attested work transition causal Command identity differs from original work evidence")
        payload[key] = event_work
    expected = Command(
        command_id=deterministic_id("od2-actor-command", owner_id, actor_id, cause),
        name=action, entity_type=ACTOR_KIND, entity_id=actor_id,
        due_at=event.occurred_at, issued_at=event.occurred_at,
        owner_id=owner_id, causation_id=None,
        correlation_id=deterministic_id("od2-actor-flow", owner_id, actor_id),
        payload=payload,
    )
    if (
        command != expected
        or event.causation_id != expected.command_id
        or (event.correlation_id is not None
            and event.correlation_id != expected.correlation_id)
        or (descendant_counts or {}).get(expected.command_id, 0) != 1
    ):
        raise ValueError("attested work transition causal Command metadata mismatch")


def _verify_actor_lifecycle_command(
    uow, *, actor_id: str, owner_id: str,
    event: DomainEvent, action: str, descendant_counts: dict[str, int],
) -> None:
    """Require one exact durable Command for each non-work StateChart edge.

    A syntactically valid activation/exit event with no real Command must
    never grant an actor eligibility. Work edges have their own stricter
    work/economic verifiers.
    """
    if not isinstance(event.causation_id, str) or not event.causation_id:
        raise ValueError("attested actor lifecycle lacks causal Command")
    command = uow.get_command(event.causation_id)
    if command is None:
        raise ValueError("attested actor lifecycle causal Command missing")
    cause = command.payload.get("external_cause_id")
    if not isinstance(cause, str) or not cause.strip():
        raise ValueError("attested actor lifecycle original cause missing")
    expected = Command(
        command_id=deterministic_id("od2-actor-command", owner_id, actor_id, cause),
        name=action, entity_type=ACTOR_KIND, entity_id=actor_id,
        due_at=event.occurred_at, issued_at=event.occurred_at,
        owner_id=owner_id, causation_id=None,
        correlation_id=deterministic_id("od2-actor-flow", owner_id, actor_id),
        payload={"external_cause_id": cause},
    )
    if (
        command != expected or event.causation_id != expected.command_id
        or (event.correlation_id is not None
            and event.correlation_id != expected.correlation_id)
        or descendant_counts.get(expected.command_id, 0) != 1
    ):
        raise ValueError("attested actor lifecycle causal Command metadata mismatch")


def _require_canonical_work_transition(event: DomainEvent, action: str) -> None:
    """Never infer a durable work grant/closure from a forged event trigger.

    The StateChart transition is canonical and owner-local. A matching
    trigger/work identity is not sufficient without the correct transition
    event name and the exact source/destination state pair.
    """
    expected = (
        ("eligible", "active") if action == "assign"
        else ("active", "eligible")
    )
    if (
        event.name != "entity.state_transition"
        or event.payload.get("trigger") != action
        or event.payload.get("from_state") != expected[0]
        or event.payload.get("to_state") != expected[1]
    ):
        raise ValueError("attested work transition shape contradicts StateChart")


def _attested_actor_replay(
    uow, *, actor_id: str, owner_id: str,
) -> tuple[DomainEvent | None, str, list[str]]:
    """Reconstruct immutable open-work, lifecycle and paid cancellation identities.

    Checking only the last assignment is unsafe: a corrupted mutable actor
    projection can authorize a second assignment without closing the first.
    Validate every actor-local open/close transition in append order before
    any new assignment or cancellation. Legacy assignments without a durable
    work ID are deliberately un-certifiable for this strict path.
    """
    # One authoritative UoW snapshot. Build per-actor causal indices ONCE:
    # repeated uow.events() deep-copied the whole global ledger for each edge.
    snapshot = uow.events()
    actor_events = tuple(
        event for event in snapshot
        if event.entity_type == ACTOR_KIND and event.entity_id == actor_id
    )
    # An immutable admitted actor cannot replay an unidentifiable work item
    # under the old pre-attestation convention. Legacy fixture-ledger replay
    # remains readable but is never upgraded to new enrollment authority.
    origin = next((
        event for event in actor_events
        if event.event_id == _calendar_origin_event_id(actor_id)
        and event.name in {"od2.actor_admitted", "od2.actor_calendar_admitted"}
    ), None)
    admitted_here = origin is not None
    causal_index: dict[str, list[DomainEvent]] = {}
    event_index: dict[str, DomainEvent] = {}
    for event in actor_events:
        original = event_index.setdefault(event.event_id, event)
        if original != event:
            raise ValueError("attested actor duplicate event identity")
        if event.causation_id is not None:
            causal_index.setdefault(event.causation_id, []).append(event)
    descendants = {cause: len(events) for cause, events in causal_index.items()}
    opened = None
    cancelled_work_ids: list[str] = []
    lifecycle = "admitted"
    # Every modern actor starts at its persisted, immutable admission instant.
    # Earlier work and lifecycle edges cannot be replayed into a legitimate
    # new actor by rewriting the mutable StateChart projection.
    last_lifecycle_at = origin.occurred_at if origin is not None else None
    last_work_at = None
    for event in actor_events:
        # The actor ID is immutable; mutable actor.owner_id is not a selector
        # for truth. Filtering by a forged owner would erase the old open
        # assignment and allow a second, cross-owner actor allocation.
        if event.owner_id != owner_id:
            raise PermissionError(
                "attested actor owner contradicts immutable event history"
            )
        action = event.payload.get("trigger")
        transitions = {
            "activate": ("admitted", "eligible"),
            "assign": ("eligible", "active"),
            "complete": ("active", "eligible"),
            "cancel_assigned": ("active", "eligible"),
            "make_unavailable": ("eligible", "unavailable"),
            "resume": ("unavailable", "eligible"),
            "begin_development": ("eligible", "development"),
            "finish_development": ("development", "eligible"),
        }
        if action in transitions or action == "exit":
            # Verify the real actor StateChart path, not only isolated
            # work labels. In particular, no assign after an exited state
            # and no assign before an admitted actor was activated.
            if action in {"assign", "complete", "cancel_assigned"}:
                _require_canonical_work_transition(event, action)
            if last_lifecycle_at is not None and event.occurred_at < last_lifecycle_at:
                raise ValueError("attested actor lifecycle causal time regressed")
            actual_from = event.payload.get("from_state")
            actual_to = event.payload.get("to_state")
            expected = transitions.get(action)
            if action == "exit":
                eligible_exit = lifecycle in {"eligible", "unavailable", "development"}
                if not eligible_exit or actual_from != lifecycle or actual_to != "exited":
                    raise ValueError("attested actor lifecycle exit contradicts immutable StateChart")
            elif expected is None or (
                lifecycle != expected[0] or actual_from != expected[0]
                or actual_to != expected[1]
            ):
                raise ValueError("attested actor lifecycle transition contradicts immutable StateChart")
            if event.name != "entity.state_transition":
                raise ValueError("attested actor lifecycle event name contradicts StateChart")
            if action not in {"assign", "complete", "cancel_assigned"}:
                _verify_actor_lifecycle_command(
                    uow, actor_id=actor_id, owner_id=owner_id,
                    event=event, action=action, descendant_counts=descendants,
                )
            lifecycle = actual_to
            last_lifecycle_at = event.occurred_at
        elif event.name == "entity.state_transition":
            raise ValueError("attested actor lifecycle transition has unknown trigger")
        if action in {"assign", "complete", "cancel_assigned"}:
            # Temporal position belongs to this actor, not a synthetic
            # organization-global clock. Append order cannot hide a
            # backdated close/assign with an otherwise valid Command.
            if last_work_at is not None and event.occurred_at < last_work_at:
                raise ValueError("attested actor work causal time regressed")
            last_work_at = event.occurred_at
        if action == "assign":
            work = event.payload.get("assigned_work_id")
            if admitted_here and (not isinstance(work, str) or not work.strip()):
                raise ValueError(
                    "immutable actor admission requires explicit assigned work identity"
                )
            # Historical actor assignments may predate immutable work IDs.
            # They can still be closed by their existing durable completion
            # event, but must never be cancelled under the new strict policy.
            if work is not None and (not isinstance(work, str) or not work.strip()):
                raise ValueError("attested original assignment work identity malformed")
            if opened is not None:
                raise ValueError("attested assignments overlap without durable closure")
            _verify_actor_work_command(
                uow, actor_id=actor_id, owner_id=owner_id,
                event=event, action="assign",
                descendant_counts=descendants,
            )
            _require_canonical_work_transition(event, "assign")
            opened = event
        elif action in {"complete", "cancel_assigned"}:
            if opened is None:
                raise ValueError("attested work completion/cancellation lacks open assignment")
            expected_id = opened.payload.get("assigned_work_id")
            if action == "cancel_assigned":
                if (
                    not isinstance(expected_id, str)
                    or event.payload.get("cancelled_work_id") != expected_id
                    or event.payload.get("source_assignment_event_id") != opened.event_id
                ):
                    raise ValueError("attested work cancellation does not close original assignment")
                # A forged fieldless/zero/boolean cost must NEVER close a
                # previously attested work item. Validate the complete durable
                # causal and economic chain BEFORE clearing open work. A
                # writable actor projection cannot stand in for this evidence.
                charged = event.payload.get("charged_cost_us")
                if type(charged) is not int or charged <= 0:
                    raise ValueError("attested work cancellation requires positive paid cost")
                if not isinstance(event.causation_id, str) or not event.causation_id:
                    raise ValueError("attested work cancellation lacks causal Command")
                command = uow.get_command(event.causation_id)
                if command is None or not isinstance(
                    command.payload.get("external_cause_id"), str
                ) or not command.payload["external_cause_id"]:
                    raise ValueError("attested work cancellation lacks paid causal evidence")
                _verify_durable_cancellation_replay(
                    uow, owner_id=owner_id, actor_id=actor_id,
                    cause_id=command.payload["external_cause_id"],
                    at=event.occurred_at, work_id=expected_id,
                    cost_us=charged,
                    causal_index=causal_index, event_index=event_index,
                )
                _require_canonical_work_transition(event, action)
                cancelled_work_ids.append(expected_id)
            # Pre-attestation assign+complete pairs may omit work IDs.
            # Once an assignment attests work, its closing completion MUST
            # attest the same explicit work ID (no default that silently
            # treats an absent completion field as matching).
            if action == "complete":
                completed_id = event.payload.get("completed_work_id")
                if expected_id is None:
                    if completed_id is not None:
                        raise ValueError("attested historical completion carries unknown work")
                elif completed_id != expected_id:
                    raise ValueError("attested work completion identity mismatch")
                _verify_actor_work_command(
                    uow, actor_id=actor_id, owner_id=owner_id,
                    event=event, action="complete",
                    descendant_counts=descendants,
                )
                _require_canonical_work_transition(event, action)
            opened = None
    return opened, lifecycle, cancelled_work_ids


def _attested_current_assignment(
    uow, *, actor_id: str, owner_id: str,
) -> DomainEvent | None:
    """Expose open-work proof to existing strict causal verifiers."""
    return _attested_actor_replay(
        uow, actor_id=actor_id, owner_id=owner_id,
    )[0]


def _attested_active_assignment(uow, *, actor_id: str, owner_id: str,
                                work_id: str) -> DomainEvent:
    source = _attested_current_assignment(
        uow, actor_id=actor_id, owner_id=owner_id,
    )
    if source is None:
        raise ValueError("attested original assignment is absent or already completed")
    if source.payload.get("assigned_work_id") != work_id:
        raise ValueError("attested original assignment work identity does not match")
    if not isinstance(source.causation_id, str):
        raise ValueError("attested original assignment lacks causal Command")
    return source


def _verify_durable_cancellation_replay(
    uow, *, owner_id: str, actor_id: str, cause_id: str,
    at: datetime, work_id: str, cost_us: int,
    causal_index: dict[str, list[DomainEvent]] | None = None,
    event_index: dict[str, DomainEvent] | None = None,
) -> None:
    """Never accept replay solely from the mutable applied_causes projection.

    The append-only cancel event carries work, cost and the original immutable
    assignment event. Its linked mutable Command must still exist unaltered;
    if it is missing/rewritten, returning success would certify missing
    economic evidence. Reject without mutating anything.
    """
    command_id = deterministic_id(
        "od2-actor-command", owner_id, actor_id, cause_id,
    )
    command = uow.get_command(command_id)
    if (
        command is None
        or command.owner_id != owner_id
        or command.entity_type != ACTOR_KIND
        or command.entity_id != actor_id
        or command.name != "cancel_assigned"
        or command.due_at != at
        or command.payload.get("external_cause_id") != cause_id
        or command.payload.get("cancelled_work_id") != work_id
        or not isinstance(command.payload.get("source_assignment_event_id"), str)
        or type(command.payload.get("charged_cost_us")) is not int
        or command.payload["charged_cost_us"] != cost_us
    ):
        raise ValueError("cancellation durable evidence: Command is missing or altered")
    candidates = (
        causal_index.get(command_id, ())
        if causal_index is not None else uow.events()
    )
    # Exactly ONE actor-local DomainEvent may descend from a paid Command.
    # Checking only matching trigger events would discard conflicting or
    # trigger-less extra descendants and falsely certify cancellation.
    descendants = [
        item for item in candidates
        if item.entity_type == ACTOR_KIND
        and item.entity_id == actor_id
        and item.causation_id == command_id
    ]
    if (
        len(descendants) != 1
        or descendants[0].owner_id != owner_id
        or descendants[0].payload.get("trigger") != "cancel_assigned"
    ):
        raise ValueError("cancellation durable evidence: unique append-only event missing")
    event = descendants[0]
    _require_canonical_work_transition(event, "cancel_assigned")
    if (
        event.occurred_at != at
        or event.payload.get("cancelled_work_id") != work_id
        or type(event.payload.get("charged_cost_us")) is not int
        or event.payload["charged_cost_us"] != cost_us
        or not isinstance(event.payload.get("source_assignment_event_id"), str)
        or event.payload["source_assignment_event_id"] != command.payload["source_assignment_event_id"]
    ):
        raise ValueError("cancellation durable evidence: event contradicts charged work")
    source_id = event.payload["source_assignment_event_id"]
    source = (
        event_index.get(source_id)
        if event_index is not None else uow.get_event(source_id)
    )
    if (
        source is None
        or source.entity_type != ACTOR_KIND
        or source.entity_id != actor_id
        or source.owner_id != owner_id
        or source.payload.get("trigger") != "assign"
        or source.payload.get("assigned_work_id") != work_id
        or source.occurred_at > at
    ):
        raise ValueError("cancellation durable evidence: original assignment missing")
    _require_canonical_work_transition(source, "assign")
    # Command rows are mutable/deletable through the persistence UoW, unlike
    # DomainEvents. Reconstruct EVERY original field from deterministic context
    # and the immutable append-only cancel event to detect silent rewrites of
    # issued_at, tick, correlation/causation, payload extras, or owner.
    expected_command = Command(
        command_id=command_id,
        name="cancel_assigned",
        entity_type=ACTOR_KIND,
        entity_id=actor_id,
        due_at=at,
        issued_at=at,
        owner_id=owner_id,
        causation_id=None,
        correlation_id=deterministic_id("od2-actor-flow", owner_id, actor_id),
        payload={
            "external_cause_id": cause_id,
            "cancelled_work_id": work_id,
            "charged_cost_us": cost_us,
            "source_assignment_event_id": source.event_id,
        },
    )
    if command != expected_command:
        raise ValueError("cancellation durable evidence: Command metadata altered")


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
            # Check owner and open-work against *all* immutable actor events,
            # before any idempotent return, not only for a new assignment.
            active_source, replayed_lifecycle, paid_cancellations = _attested_actor_replay(
                uow, actor_id=actor_id, owner_id=owner_id,
            )
            if actor.state != replayed_lifecycle:
                raise ValueError("actor projection contradicts immutable lifecycle history")
            if meta.get("cancelled_work_ids") != paid_cancellations:
                raise ValueError("actor cancellation projection contradicts immutable paid history")
            if active_source is None:
                if meta["assigned_work_id"] is not None:
                    raise ValueError("actor projection contains work absent from immutable history")
            else:
                attested_work = active_source.payload.get("assigned_work_id")
                if (
                    actor.state != "active"
                    or meta.get("assigned_work_id") is None
                    or (attested_work is not None
                        and meta["assigned_work_id"] != attested_work)
                ):
                    raise ValueError("actor projection contradicts immutable active work")
            used = meta["applied_causes"].get(cause_id)
            if used is not None:
                if used != list(identity):
                    raise ValueError("actor cause was replayed with different action")
                if action == "cancel_assigned":
                    _verify_durable_cancellation_replay(
                        uow, owner_id=owner_id, actor_id=actor_id,
                        cause_id=cause_id, at=now, work_id=work_id,
                        cost_us=cost_us,
                    )
                return actor
            prior_at = datetime.fromisoformat(meta["last_at"])
            if now < prior_at:
                raise ValueError("actor effective time regressed")
            assignment_source = None
            if action == "assign":
                _require_actor_calendar_start(uow, actor, now, require_window=True)
                _identity(work_id, "work_id")
                # Reject a second assignment if append-only history still
                # contains one unclosed, regardless of mutable actor state.
                if active_source is not None:
                    raise ValueError("actor already assigned: attested assignment remains active")
                if meta["assigned_work_id"] is not None:
                    raise ValueError("actor already assigned")
            elif action == "complete":
                if work_id is None or work_id != meta["assigned_work_id"]:
                    raise ValueError("completion requires the matching assigned work")
                # Do not let a forged mutable projection close the wrong
                # immutable assignment. Historical pre-attestation work
                # remains eligible for its legacy completion semantics.
                if active_source is None:
                    raise ValueError("completion requires an attested active assignment")
                source_work = active_source.payload.get("assigned_work_id")
                if source_work is not None and source_work != work_id:
                    raise ValueError("attested original assignment work identity does not match")
            elif action == "cancel_assigned":
                # A paid, explicit actor-local cancellation. It never
                # releases a domain ScheduledWork or a PhysicalReservation:
                # those need a separately certified authority (#477).
                if work_id is None or work_id != meta["assigned_work_id"]:
                    raise ValueError("cancellation requires the matching assigned work")
                if cost_us <= 0:
                    raise ValueError("cancellation requires explicit paid cost")
                assignment_source = _attested_active_assignment(
                    uow, actor_id=actor_id, owner_id=owner_id,
                    work_id=work_id,
                )
            elif work_id is not None:
                raise ValueError("work identity is only legal for assign/complete")
            if action == "begin_development" and cost_us == 0:
                raise ValueError("development must have explicit paid cost")

            engine = _engine(store, owner_id=owner_id, at=now)
            command = Command(
                command_id=deterministic_id("od2-actor-command", owner_id, actor_id, cause_id),
                name=action, entity_type=ACTOR_KIND, entity_id=actor_id,
                due_at=now, issued_at=now, owner_id=owner_id,
                # A supplied external observation is provenance, not a
                # phantom persisted DomainEvent causal parent. The emitted
                # StateChart event points instead at this persisted Command.
                causation_id=None,
                correlation_id=deterministic_id("od2-actor-flow", owner_id, actor_id),
                payload={
                    "external_cause_id": cause_id,
                    **({"assigned_work_id": work_id} if action == "assign" else {}),
                    **({
                        "completed_work_id": work_id,
                    } if action == "complete" and active_source is not None
                    and active_source.payload.get("assigned_work_id") is not None else {}),
                    **({
                        "cancelled_work_id": work_id,
                        "charged_cost_us": cost_us,
                        "source_assignment_event_id": assignment_source.event_id,
                    } if action == "cancel_assigned" else {}),
                },
            )
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
                if action in {"complete", "cancel_assigned"}:
                    meta["assigned_work_id"] = None
                if action == "cancel_assigned":
                    meta.setdefault("cancelled_work_ids", []).append(work_id)
                uow.save_entity(actor)
                uow.save_command(command)
                for event in emitted:
                    # The DomainEvent is append-only causal/economic truth.
                    # Commands and entity projections may be rewritten through
                    # UoW APIs; neither alone is cancellation attestation.
                    extra = {}
                    if action == "assign":
                        extra = {"assigned_work_id": work_id}
                    elif action == "complete":
                        # A pre-attestation in-flight assignment has no
                        # immutable work ID; do not encode a modern close
                        # record contradicting its legacy source. Modern
                        # assignments always require exact close IDs.
                        if active_source is not None and (
                            active_source.payload.get("assigned_work_id") is not None
                        ):
                            extra = {"completed_work_id": work_id}
                    elif action == "cancel_assigned":
                        extra = {
                            "cancelled_work_id": work_id,
                            "charged_cost_us": cost_us,
                            "source_assignment_event_id": assignment_source.event_id,
                        }
                    uow.append_event(replace(event, payload={**event.payload, **extra}))
                return actor
            finally:
                engine.context.drain_events()

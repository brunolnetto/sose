"""OD-3 real queue decisions: follow-up, cherry picking and charged sham.

An isolated deterministic scheduler contract, not a claim that A1 BATCHING
implements these behaviors. Durable item/actor ledger integration remains #463.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("OD-3 decision requires timezone-aware observation time")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class QueuedWork:
    item_id: str
    owner_id: str
    arrived_at: datetime
    service_us: int
    last_contact_at: datetime | None = None
    blocked: bool = False

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in (self.item_id, self.owner_id)
        ):
            raise ValueError("work identity and owner must be canonical nonblank strings")
        if type(self.service_us) is not int or self.service_us <= 0:
            raise ValueError("service effort must be a positive integer")
        _utc(self.arrived_at)
        if self.last_contact_at is not None:
            _utc(self.last_contact_at)


@dataclass(frozen=True, slots=True)
class OD3Policy:
    name: str
    delay_threshold_us: int
    cooldown_us: int
    decision_cost_us: int
    starvation_bound_us: int
    matched_policy: str | None = None

    def __post_init__(self) -> None:
        if self.name not in ("adaptive_follow_up", "cherry_picking", "sham"):
            raise ValueError("unsupported operational OD-3 policy")
        if self.name == "sham":
            if self.matched_policy not in ("adaptive_follow_up", "cherry_picking"):
                raise ValueError("sham must declare its matched real policy")
        elif self.matched_policy is not None:
            raise ValueError("matched_policy is valid only on sham decisions")
        for key in ("delay_threshold_us", "cooldown_us", "decision_cost_us", "starvation_bound_us"):
            value = getattr(self, key)
            if type(value) is not int or value < 0:
                raise ValueError("OD-3 policy bounds/cost must be nonnegative integer")
        if self.starvation_bound_us < self.delay_threshold_us:
            raise ValueError("starvation guard must cover policy trigger")


@dataclass(frozen=True, slots=True)
class OD3Decision:
    owner_id: str
    policy: str
    observed_at: datetime
    observation_id: str
    matched_policy: str
    decision_id: str
    action: str
    selected_item_id: str | None
    before: tuple[str, ...]
    after: tuple[str, ...]
    cost_us: int
    reason: str
    snapshot_id: str
    policy_config_id: str


def _decision_identity_namespace(policy_name: str, matched_policy: str) -> str:
    """Preserve v1 cherry IDs; version the changed fair follow-up semantics.

    Old persisted v1 follow-up evidence remains immutable, but cannot silently
    be reused as if it had the new contact selection and reason fields.
    """
    effective = matched_policy if policy_name == "sham" else policy_name
    return (
        "od3-decision-fair-followup-v2"
        if effective == "adaptive_follow_up"
        else "od3-decision"
    )


def _policy_config_id(policy: OD3Policy) -> str:
    return deterministic_id(
        "od3-policy-config-v1", policy.name, policy.delay_threshold_us,
        policy.cooldown_us, policy.decision_cost_us, policy.starvation_bound_us,
    )


def decide_od3(
    *, owner_id: str, queue: tuple[QueuedWork, ...], policy: OD3Policy,
    observed_at: datetime, previous_decision_at: datetime | None = None,
    observation_id: str, matched_active_policy: OD3Policy | None = None,
) -> OD3Decision:
    """Observe only arrived items; produce a durable-ready deterministic action.

    A successful policy decision changes queue priority or records a follow-up
    contact. Cherry-picking preferentially selects short jobs until the oldest
    eligible item reaches a fixed starvation bound, then MUST favor that item.
    A matched-cost sham emits no queue reorder or contact.
    """
    now = _utc(observed_at)
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("OD-3 owner must be nonblank")
    if not isinstance(observation_id, str) or not observation_id.strip():
        raise ValueError("OD-3 observation identity required")
    if not isinstance(queue, tuple):
        raise ValueError("queue must be an immutable tuple")
    if len({item.item_id for item in queue}) != len(queue):
        raise ValueError("duplicate work identity")
    if any(item.owner_id != owner_id for item in queue):
        raise PermissionError("OD-3 queue contains foreign owner")
    if any(_utc(item.arrived_at) > now or (
        item.last_contact_at is not None and _utc(item.last_contact_at) > now
    ) for item in queue):
        raise ValueError("OD-3 decision cannot observe future queue history")
    if previous_decision_at is not None and _utc(previous_decision_at) > now:
        raise ValueError("OD-3 previous decision occurs in the future")
    if policy.name == "sham":
        if matched_active_policy is None or matched_active_policy.name != policy.matched_policy:
            raise ValueError("sham requires explicit matched active policy configuration")
        if (
            policy.delay_threshold_us, policy.cooldown_us, policy.decision_cost_us,
            policy.starvation_bound_us,
        ) != (
            matched_active_policy.delay_threshold_us, matched_active_policy.cooldown_us,
            matched_active_policy.decision_cost_us, matched_active_policy.starvation_bound_us,
        ):
            raise ValueError("sham and active policy configuration mismatch")
        active_policy = matched_active_policy
    else:
        if matched_active_policy is not None:
            raise ValueError("active policy cannot declare a matched policy")
        active_policy = policy
    policy_config_id = _policy_config_id(active_policy)
    snapshot_id = deterministic_id(
        "od3-queue-snapshot-v1", owner_id, now.isoformat(),
        _utc(previous_decision_at).isoformat() if previous_decision_at else None,
        tuple((item.item_id, item.owner_id, _utc(item.arrived_at).isoformat(),
               item.service_us,
               _utc(item.last_contact_at).isoformat() if item.last_contact_at else None,
               item.blocked) for item in queue),
    )
    before = tuple(item.item_id for item in queue)
    decision_id = deterministic_id(
        _decision_identity_namespace(policy.name, policy.matched_policy or policy.name),
        owner_id, observation_id, policy.name,
        policy.matched_policy or policy.name, snapshot_id, policy_config_id,
    )
    action, selected, reason, cost, after = "NOOP", None, "not triggered", 0, before
    if previous_decision_at is not None and (
        now - _utc(previous_decision_at) < timedelta(microseconds=policy.cooldown_us)
    ):
        reason = "decision cooldown"
    else:
        effective_policy = policy.matched_policy if policy.name == "sham" else policy.name
        eligible = tuple(item for item in queue if not item.blocked)
        waiting = tuple(item for item in eligible if (
            now - _utc(item.arrived_at) >= timedelta(microseconds=policy.delay_threshold_us)
        ))
        if effective_policy == "adaptive_follow_up":
            # Filter cooling items first: the next oldest eligible item can
            # still be contacted while a previously contacted item cools.
            waiting = tuple(item for item in waiting if (
                item.last_contact_at is None
                or now - _utc(item.last_contact_at) >= timedelta(
                    microseconds=policy.cooldown_us,
                )
            ))
        if waiting:
            oldest = min(waiting, key=lambda item: (_utc(item.arrived_at), item.item_id))
            target = oldest
            if effective_policy == "cherry_picking":
                if now - _utc(oldest.arrived_at) < timedelta(
                    microseconds=policy.starvation_bound_us,
                ):
                    target = min(waiting, key=lambda x: (
                        x.service_us, _utc(x.arrived_at), x.item_id,
                    ))
                    reason = "shortest eligible service"
                else:
                    reason = "starvation guard: oldest eligible work"
            else:
                # Across repeated paid contacts, never-contacted work takes
                # precedence over repeatedly contacted older work. Among
                # contacted items, the least recently contacted goes first.
                # This prevents one permanently overdue item from monopolizing
                # the finite contact budget after its cooldown expires.
                target = min(waiting, key=lambda item: (
                    item.last_contact_at is not None,
                    _utc(item.last_contact_at) if item.last_contact_at is not None
                    else _utc(item.arrived_at),
                    _utc(item.arrived_at), item.item_id,
                ))
                reason = "uncontacted or least-recently-contacted overdue work"
            if target is not None:
                selected = target.item_id
                cost = policy.decision_cost_us
                if policy.name == "sham":
                    action = "SHAM"
                elif policy.name == "adaptive_follow_up":
                    action = "CONTACT"
                else:
                    action = "PRIORITIZE"
                    after = (selected, *(key for key in before if key != selected))
    return OD3Decision(
        owner_id, policy.name, now, observation_id,
        policy.matched_policy or policy.name, decision_id,
        action, selected, before, tuple(after), cost, reason,
        snapshot_id, policy_config_id,
    )


def _validate_decision_evidence(decision: OD3Decision) -> None:
    """Refuse contradictory observational evidence, without granting authority.

    This validates the shape of the *claimed* action. Only an authorized
    scheduler/actor ledger can establish that an effect or paid cost occurred.
    """
    if not isinstance(decision, OD3Decision):
        raise TypeError("OD-3 decision evidence must be an OD3Decision")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (
            decision.owner_id, decision.observation_id, decision.decision_id,
            decision.snapshot_id, decision.policy_config_id, decision.reason,
        )
    ):
        raise ValueError("OD-3 decision evidence has an invalid identity or reason")
    _utc(decision.observed_at)
    before, after = decision.before, decision.after
    if (
        not isinstance(before, tuple) or not isinstance(after, tuple)
        or any(not isinstance(item_id, str) or not item_id.strip()
               for item_id in before + after)
        or len(set(before)) != len(before)
        or len(set(after)) != len(after)
        or set(before) != set(after)
    ):
        raise ValueError("OD-3 decision evidence changes the queue membership")
    if type(decision.cost_us) is not int or decision.cost_us < 0:
        raise ValueError("OD-3 decision evidence requires nonnegative integer cost")
    if decision.policy == "sham":
        if decision.matched_policy not in ("adaptive_follow_up", "cherry_picking"):
            raise ValueError("OD-3 decision evidence has an invalid sham pairing")
    elif (
        decision.policy not in ("adaptive_follow_up", "cherry_picking")
        or decision.matched_policy != decision.policy
    ):
        raise ValueError("OD-3 decision evidence has an invalid policy pairing")

    if decision.action == "NOOP":
        if decision.selected_item_id is not None or decision.cost_us or after != before:
            raise ValueError("OD-3 decision evidence has an effectful NOOP")
        return
    if decision.selected_item_id not in before:
        raise ValueError("OD-3 decision evidence selects absent work")
    if decision.action == "CONTACT":
        valid = decision.policy == "adaptive_follow_up" and after == before
    elif decision.action == "SHAM":
        valid = decision.policy == "sham" and after == before
    elif decision.action == "PRIORITIZE":
        expected = (
            decision.selected_item_id,
            *(item_id for item_id in before if item_id != decision.selected_item_id),
        )
        valid = decision.policy == "cherry_picking" and after == expected
    else:
        valid = False
    if not valid:
        raise ValueError("OD-3 decision evidence contradicts its policy action")


def od3_decision_event(decision: OD3Decision) -> DomainEvent:
    """Construct deterministic decision evidence for a caller-owned atomic UoW.

    This event is diagnostic unless accompanied by an actual committed effect.
    """
    _validate_decision_evidence(decision)
    if decision.decision_id != deterministic_id(
        _decision_identity_namespace(decision.policy, decision.matched_policy),
        decision.owner_id, decision.observation_id,
        decision.policy, decision.matched_policy,
        decision.snapshot_id, decision.policy_config_id,
    ):
        raise ValueError("OD-3 durable decision identity contradicts observation")
    event = DomainEvent(
        event_id=decision.decision_id, name="od3.policy_decided",
        entity_type="organizational_policy", entity_id=decision.owner_id,
        occurred_at=decision.observed_at, owner_id=decision.owner_id,
        correlation_id=deterministic_id(
            "od3-observation", decision.owner_id, decision.observation_id,
            decision.snapshot_id,
        ),
        payload={
            "observation_id": decision.observation_id,
            "snapshot_id": decision.snapshot_id,
            "policy_config_id": decision.policy_config_id,
            "matched_policy": decision.matched_policy,
            "policy": decision.policy, "action": decision.action,
            "selected_item_id": decision.selected_item_id,
            "before": list(decision.before), "after": list(decision.after),
            "cost_us": decision.cost_us, "reason": decision.reason,
        },
    )
    return event


def persist_od3_decision(store, decision: OD3Decision) -> DomainEvent:
    """Persist diagnostic (non-authorizing) OD-3 decision evidence idempotently."""
    event = od3_decision_event(decision)
    with store.transaction(owner_id=decision.owner_id) as uow:
        original = uow.get_event(event.event_id)
        if original is not None:
            if original != event:
                raise ValueError("OD-3 decision identity replay conflict")
            return original
        uow.append_event(event)
    return event

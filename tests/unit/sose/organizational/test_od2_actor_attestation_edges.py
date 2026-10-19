"""Exact causal attestation edge falsifiers for bounded OD-2 cancellation.

All tests drive production verifiers with explicitly constructed immutable
DomainEvents and Commands, not a mock of their actual implementation.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import (
    ACTOR_KIND,
    _attested_active_assignment,
    _attested_current_assignment,
    _verify_durable_cancellation_replay,
)

NOW = datetime(2026, 10, 18, 13, tzinfo=timezone.utc)
OWNER = "org:attested"
ACTOR = "actor:immutable"
WORK = "work:A"
CAUSE = "cause:cancel"


def event(trigger, *, event_id=None, owner_id=OWNER, actor_id=ACTOR,
          at=NOW, **payload):
    ident = event_id or "event:" + trigger
    from_state, to_state = {
        "assign": ("eligible", "active"),
        "complete": ("active", "eligible"),
        "cancel_assigned": ("active", "eligible"),
        "activate": ("admitted", "eligible"),
    }.get(trigger, ("eligible", "eligible"))
    return DomainEvent(
        event_id=ident,
        name="entity.state_transition", entity_type=ACTOR_KIND, entity_id=actor_id,
        occurred_at=at, owner_id=owner_id,
        causation_id=deterministic_id("od2-actor-command", owner_id, actor_id, ident),
        correlation_id=deterministic_id("od2-actor-flow", owner_id, actor_id),
        payload={"trigger": trigger, "from_state": from_state,
                 "to_state": to_state, **payload},
    )


def issued_command_for(event):
    """Explicitly materialize an issued test Command for a source transition.

    The service's verifier, not this fixture, decides whether its fields
    actually attest the event. A cancelled work command is supplied separately.
    """
    action = event.payload.get("trigger")
    if action not in {
        "activate", "assign", "complete", "cancel_assigned", "exit",
        "make_unavailable", "resume", "begin_development",
        "finish_development",
    }:
        return None
    payload = {"external_cause_id": event.event_id}
    key = (
        "assigned_work_id" if action == "assign"
        else "completed_work_id" if action == "complete" else None
    )
    if key is not None and key in event.payload:
        payload[key] = event.payload[key]
    return Command(
        command_id=deterministic_id(
            "od2-actor-command", event.owner_id, event.entity_id, event.event_id,
        ),
        name=action,
        entity_type=ACTOR_KIND,
        entity_id=event.entity_id,
        owner_id=event.owner_id,
        due_at=event.occurred_at,
        issued_at=event.occurred_at,
        correlation_id=deterministic_id(
            "od2-actor-flow", event.owner_id, event.entity_id,
        ),
        payload=payload,
    )


class Ledger:
    def __init__(self, events=(), command=None, source=None, *, seed_lifecycle=True):
        history = tuple(events)
        if seed_lifecycle and not any(
            item.entity_type == ACTOR_KIND and item.entity_id == ACTOR
            and item.payload.get("trigger") == "activate"
            for item in history
        ):
            history = (event(
                "activate", event_id="admitted-to-eligible",
                at=NOW - timedelta(days=7),
            ), *history)
        self._events = history
        self._command = command
        self._source = source

    def events(self):
        return self._events

    def get_command(self, command_id):
        if self._command is not None and self._command.command_id == command_id:
            return self._command
        matches = [
            issued_command_for(item) for item in self._events
            if item.payload.get("trigger") in {
                "activate", "assign", "complete", "exit",
                "make_unavailable", "resume", "begin_development",
                "finish_development",
            } and item.causation_id == command_id
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def get_event(self, event_id):
        if self._source is not None and self._source.event_id == event_id:
            return self._source
        return None


def test_empty_and_non_actor_events_do_not_create_active_work():
    irrelevant = event(
        "assign", actor_id="actor:peer", assigned_work_id="work:peer",
    )
    ledger = Ledger([irrelevant, event("activate", event_id="other")])
    assert _attested_current_assignment(
        ledger, actor_id=ACTOR, owner_id=OWNER,
    ) is None
    with pytest.raises(ValueError, match="original assignment"):
        _attested_active_assignment(
            ledger, actor_id=ACTOR, owner_id=OWNER, work_id=WORK,
        )


@pytest.mark.parametrize("kind,pattern", [
    ("wrong_owner", "owner"),
    ("overlap", "overlap|lifecycle"),
    ("blank_work", "malformed"),
    ("numeric_work", "malformed"),
    ("stray_complete", "lacks open assignment|lifecycle"),
    ("stray_cancel", "lacks open assignment|lifecycle"),
    ("missing_complete_id", "completion identity mismatch"),
    ("wrong_complete_id", "completion identity mismatch"),
    ("legacy_with_new_complete", "historical completion"),
    ("cancel_wrong_work", "cancellation does not close"),
    ("cancel_wrong_source", "cancellation does not close"),
    ("cancel_without_work_identity", "cancellation does not close"),
])
def test_full_work_history_rejects_corruption(kind, pattern):
    opened = event(
        "assign", event_id="original", at=NOW - timedelta(hours=1),
        assigned_work_id=WORK,
    )
    complete = event("complete", event_id="closed", completed_work_id=WORK)
    cancelled = event(
        "cancel_assigned", event_id="cancelled",
        cancelled_work_id=WORK, source_assignment_event_id="original",
    )
    if kind == "wrong_owner":
        events = [replace(opened, owner_id="org:forged")]
    elif kind == "overlap":
        # Two separately issued Commands, each causally valid, cannot
        # authorize overlapping actor-local assignments.
        events = [
            opened,
            event("assign", event_id="second", assigned_work_id="work:B"),
        ]
    elif kind == "blank_work":
        events = [replace(opened, payload={**opened.payload, "assigned_work_id": ""})]
    elif kind == "numeric_work":
        events = [replace(opened, payload={**opened.payload, "assigned_work_id": 3})]
    elif kind == "stray_complete":
        events = [complete]
    elif kind == "stray_cancel":
        events = [cancelled]
    elif kind == "missing_complete_id":
        events = [opened, replace(complete, payload={
            k: v for k, v in complete.payload.items() if k != "completed_work_id"
        })]
    elif kind == "wrong_complete_id":
        events = [opened, replace(
            complete, payload={**complete.payload, "completed_work_id": "work:B"},
        )]
    elif kind == "legacy_with_new_complete":
        events = [replace(opened, payload={
            k: v for k, v in opened.payload.items() if k != "assigned_work_id"
        }), complete]
    elif kind == "cancel_wrong_work":
        events = [opened, replace(cancelled, payload={
            **cancelled.payload, "cancelled_work_id": "work:B",
        })]
    elif kind == "cancel_wrong_source":
        events = [opened, replace(cancelled, payload={
            **cancelled.payload, "source_assignment_event_id": "fake",
        })]
    else:
        events = [replace(opened, payload={
            k: v for k, v in opened.payload.items() if k != "assigned_work_id"
        }), cancelled]
    with pytest.raises((PermissionError, ValueError), match=pattern):
        _attested_current_assignment(
            Ledger(events), actor_id=ACTOR, owner_id=OWNER,
        )


def test_valid_modern_and_legacy_work_sequences_have_only_one_open_assignment():
    modern = event(
        "assign", event_id="modern-assign", assigned_work_id=WORK,
    )
    modern_closed = event(
        "complete", event_id="modern-complete", completed_work_id=WORK,
    )
    legacy = event("assign", event_id="legacy-assign")
    legacy_closed = event("complete", event_id="legacy-complete")
    fresh = event(
        "assign", event_id="fresh", assigned_work_id="work:B",
    )
    ledger = Ledger([modern, modern_closed, legacy, legacy_closed, fresh])
    assert _attested_current_assignment(
        ledger, actor_id=ACTOR, owner_id=OWNER,
    ).event_id == "fresh"
    with pytest.raises(ValueError, match="work identity"):
        _attested_active_assignment(
            ledger, actor_id=ACTOR, owner_id=OWNER, work_id=WORK,
        )
    assert _attested_active_assignment(
        ledger, actor_id=ACTOR, owner_id=OWNER, work_id="work:B",
    ).event_id == "fresh"


def test_active_assignment_requires_source_causation_even_with_matching_work():
    without_parent = event(
        "assign", assigned_work_id=WORK,
    )
    with pytest.raises(ValueError, match="causal Command"):
        _attested_active_assignment(
            Ledger([replace(without_parent, causation_id=None)]),
            actor_id=ACTOR, owner_id=OWNER, work_id=WORK,
        )


def valid_replay():
    source = event(
        "assign", event_id="original", at=NOW - timedelta(hours=1),
        assigned_work_id=WORK,
    )
    cmd_id = deterministic_id(
        "od2-actor-command", OWNER, ACTOR, CAUSE,
    )
    command = Command(
        command_id=cmd_id,
        name="cancel_assigned", entity_type=ACTOR_KIND,
        entity_id=ACTOR, due_at=NOW, issued_at=NOW, owner_id=OWNER,
        causation_id=None,
        correlation_id=deterministic_id("od2-actor-flow", OWNER, ACTOR),
        payload={
            "external_cause_id": CAUSE,
            "cancelled_work_id": WORK,
            "charged_cost_us": 700,
            "source_assignment_event_id": source.event_id,
        },
    )
    done = event(
        "cancel_assigned", event_id="done",
        cancelled_work_id=WORK,
        charged_cost_us=700,
        source_assignment_event_id=source.event_id,
    )
    done = replace(done, causation_id=cmd_id)
    return command, source, done


def verify(ledger):
    return _verify_durable_cancellation_replay(
        ledger, owner_id=OWNER, actor_id=ACTOR, cause_id=CAUSE,
        at=NOW, work_id=WORK, cost_us=700,
    )


def test_verified_complete_economic_replay_passes():
    command, source, done = valid_replay()
    assert verify(Ledger([source, done], command, source)) is None


@pytest.mark.parametrize("kind,pattern", [
    ("command_missing", "Command is missing"),
    ("command_owner", "Command is missing"),
    ("command_work", "Command is missing"),
    ("command_cost", "Command is missing"),
    ("command_timestamp", "Command metadata"),
    ("command_payload_extra", "Command metadata"),
    ("event_missing", "event missing"),
    ("event_duplicate", "event missing"),
    ("event_wrong_cost", "event contradicts"),
    ("event_wrong_work", "event contradicts"),
    ("event_wrong_source", "event contradicts"),
    ("event_wrong_timestamp", "event contradicts"),
    ("source_missing", "original assignment missing"),
    ("source_wrong_owner", "original assignment missing"),
    ("source_wrong_actor", "original assignment missing"),
    ("source_wrong_work", "original assignment missing"),
    ("source_wrong_trigger", "original assignment missing"),
    ("source_in_future", "original assignment missing"),
])
def test_paid_cancellation_replay_rejects_each_corrupted_boundary(kind, pattern):
    command, source, done = valid_replay()
    events = [source, done]
    if kind == "command_missing":
        command = None
    elif kind == "command_owner":
        command = replace(command, owner_id="org:forged")
    elif kind == "command_work":
        command = replace(command, payload={
            **command.payload, "cancelled_work_id": "work:B",
        })
    elif kind == "command_cost":
        command = replace(command, payload={
            **command.payload, "charged_cost_us": 1,
        })
    elif kind == "command_timestamp":
        command = replace(command, issued_at=NOW + timedelta(seconds=1))
    elif kind == "command_payload_extra":
        command = replace(command, payload={
            **command.payload, "extra": True,
        })
    elif kind == "event_missing":
        events = [source]
    elif kind == "event_duplicate":
        events = [source, done, replace(done, event_id="duplicate")]
    elif kind == "event_wrong_cost":
        events = [source, replace(done, payload={
            **done.payload, "charged_cost_us": 1,
        })]
    elif kind == "event_wrong_work":
        events = [source, replace(done, payload={
            **done.payload, "cancelled_work_id": "work:B",
        })]
    elif kind == "event_wrong_source":
        events = [source, replace(done, payload={
            **done.payload, "source_assignment_event_id": "wrong",
        })]
    elif kind == "event_wrong_timestamp":
        events = [source, replace(done, occurred_at=NOW + timedelta(seconds=1))]
    elif kind == "source_missing":
        source = None
    elif kind == "source_wrong_owner":
        source = replace(source, owner_id="org:forged")
    elif kind == "source_wrong_actor":
        source = replace(source, entity_id="actor:forged")
    elif kind == "source_wrong_work":
        source = replace(source, payload={
            **source.payload, "assigned_work_id": "work:B",
        })
    elif kind == "source_wrong_trigger":
        source = replace(source, payload={"trigger": "activate"})
    elif kind == "source_in_future":
        source = replace(source, occurred_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match=pattern):
        verify(Ledger(events, command, source))



def test_authoritative_paid_cancel_closes_one_attested_work():
    command, source, done = valid_replay()
    next_work = event("assign", event_id="next", assigned_work_id="work:B")
    ledger = Ledger([source, done, next_work], command, source)
    assert _attested_current_assignment(
        ledger, actor_id=ACTOR, owner_id=OWNER,
    ).event_id == "next"


@pytest.mark.parametrize("bad_cost", (None, 0, -1, True, 1.5, "700"))
def test_unpaid_or_malformed_cancel_never_closes_work(bad_cost):
    """An attacker must not use a no-cost close to open another assignment."""
    command, source, done = valid_replay()
    modified = dict(done.payload)
    if bad_cost is None:
        modified.pop("charged_cost_us")
    else:
        modified["charged_cost_us"] = bad_cost
    done = replace(done, payload=modified)
    next_work = event("assign", event_id="work:B", assigned_work_id="work:B")
    ledger = Ledger([source, done, next_work], command, source)
    with pytest.raises(ValueError, match="positive paid cost"):
        _attested_current_assignment(
            ledger, actor_id=ACTOR, owner_id=OWNER,
        )


@pytest.mark.parametrize("tamper", (
    "missing_causation", "empty_causation", "missing_command",
    "missing_external_cause", "wrong_external_cause", "altered_command",
    "wrong_source_evidence", "wrong_event_time",
))
def test_cancel_does_not_close_work_without_complete_causal_evidence(tamper):
    command, source, done = valid_replay()
    if tamper == "missing_causation":
        done = replace(done, causation_id=None)
    elif tamper == "empty_causation":
        done = replace(done, causation_id="")
    elif tamper == "missing_command":
        command = None
    elif tamper == "missing_external_cause":
        command = replace(command, payload={
            k: v for k, v in command.payload.items() if k != "external_cause_id"
        })
    elif tamper == "wrong_external_cause":
        command = replace(command, payload={
            **command.payload, "external_cause_id": "forged-cause",
        })
    elif tamper == "altered_command":
        command = replace(command, correlation_id="forged")
    elif tamper == "wrong_source_evidence":
        done = replace(done, payload={
            **done.payload, "source_assignment_event_id": "nonexistent",
        })
    else:
        done = replace(done, occurred_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cancellation|causal"):
        _attested_current_assignment(
            Ledger([source, done], command, source),
            actor_id=ACTOR, owner_id=OWNER,
        )



@pytest.mark.parametrize("last_action", ("complete", "assign"))
def test_actor_work_history_rejects_backdated_causal_transition(last_action):
    """Independent actor-local event time cannot run backward across work."""
    opened = event(
        "assign", event_id="opened", at=NOW, assigned_work_id=WORK,
    )
    if last_action == "complete":
        later = event(
            "complete", event_id="backdated-complete",
            at=NOW - timedelta(hours=1), completed_work_id=WORK,
        )
        events = [opened, later]
    else:
        closed = event(
            "complete", event_id="closed", at=NOW, completed_work_id=WORK,
        )
        later = event(
            "assign", event_id="backdated-assign",
            at=NOW - timedelta(hours=1), assigned_work_id="work:B",
        )
        events = [opened, closed, later]
    with pytest.raises(ValueError, match="causal time regressed"):
        _attested_current_assignment(
            Ledger(events), owner_id=OWNER, actor_id=ACTOR,
        )



@pytest.mark.parametrize("corruption", (
    "command_missing", "command_action", "command_owner", "command_entity",
    "command_due_at", "command_issued_at", "command_correlation",
    "command_causation", "command_extra_payload", "command_work",
    "command_external_cause", "event_wrong_correlation", "event_wrong_causation",
    "duplicate_command_descendant",
))
def test_source_assignment_rejects_corrupted_or_reused_exact_command(corruption):
    source = event(
        "assign", event_id="real-source",
        assigned_work_id=WORK,
    )
    command = issued_command_for(source)
    assert command is not None
    events = [source]
    if corruption == "command_missing":
        command = None
    elif corruption == "command_action":
        command = replace(command, name="activate")
    elif corruption == "command_owner":
        command = replace(command, owner_id="org:other")
    elif corruption == "command_entity":
        command = replace(command, entity_id="actor:other")
    elif corruption == "command_due_at":
        command = replace(command, due_at=NOW + timedelta(seconds=1))
    elif corruption == "command_issued_at":
        command = replace(command, issued_at=NOW + timedelta(seconds=1))
    elif corruption == "command_correlation":
        command = replace(command, correlation_id="forged")
    elif corruption == "command_causation":
        command = replace(command, causation_id="forged")
    elif corruption == "command_extra_payload":
        command = replace(command, payload={
            **command.payload, "authority_override": True,
        })
    elif corruption == "command_work":
        command = replace(command, payload={
            **command.payload, "assigned_work_id": "work:forged",
        })
    elif corruption == "command_external_cause":
        command = replace(command, payload={
            **command.payload, "external_cause_id": "wrong-cause",
        })
    elif corruption == "event_wrong_correlation":
        source = replace(source, correlation_id="forged")
        events = [source]
    elif corruption == "event_wrong_causation":
        source = replace(source, causation_id="forged")
        events = [source]
    else:
        events = [source, replace(source, event_id="duplicate-event")]
    ledger = Ledger(events, command)
    if corruption == "command_missing":
        # Disable the unit fixture's normal source Command construction.
        # Preserve the valid activation Command: falsify ONLY the target
        # assignment Command, not the earlier lifecycle prerequisite.
        real_get_command = ledger.get_command
        ledger.get_command = lambda key: (
            None if key == source.causation_id else real_get_command(key)
        )
    with pytest.raises(ValueError, match="attested work"):
        _attested_current_assignment(
            ledger, owner_id=OWNER, actor_id=ACTOR,
        )



@pytest.mark.parametrize("action,field,value", [
    ("assign", "name", "od2.assign"),
    ("assign", "from_state", "active"),
    ("assign", "to_state", "eligible"),
    ("complete", "name", "od2.complete"),
    ("complete", "from_state", "eligible"),
    ("complete", "to_state", "active"),
    ("cancel_assigned", "name", "od2.cancel_assigned"),
    ("cancel_assigned", "from_state", "eligible"),
    ("cancel_assigned", "to_state", "active"),
])
def test_forged_transition_shape_never_opens_or_closes_attested_work(action, field, value):
    """P1: matching trigger/work/cause cannot authorize an invalid StateChart edge."""
    command, source, closed = valid_replay()
    completed = event("complete", event_id="complete", completed_work_id=WORK)
    if action == "assign":
        if field == "name":
            source = replace(source, name=value)
        else:
            source = replace(source, payload={**source.payload, field: value})
        ledger = Ledger([source], issued_command_for(source), source)
    elif action == "complete":
        if field == "name":
            completed = replace(completed, name=value)
        else:
            completed = replace(completed, payload={**completed.payload, field: value})
        ledger = Ledger([source, completed], None, source)
    else:
        if field == "name":
            closed = replace(closed, name=value)
        else:
            closed = replace(closed, payload={**closed.payload, field: value})
        ledger = Ledger([source, closed], command, source)
    with pytest.raises(ValueError, match="work transition shape|attested work"):
        _attested_current_assignment(ledger, actor_id=ACTOR, owner_id=OWNER)



@pytest.mark.parametrize("violation", ("before_activate", "after_exit", "bad_resume", "bad_activation"))
def test_actor_work_identity_requires_valid_full_lifecycle_history(violation):
    """P1: an exact work Command after exit or before activate never reopens an actor."""
    opened = event("assign", event_id="work:after", assigned_work_id=WORK)
    if violation == "before_activate":
        events = [opened]
    elif violation == "after_exit":
        activated = event(
            "activate", event_id="activated",
            at=NOW - timedelta(hours=3),
        )
        exited = event(
            "exit", event_id="exited", at=NOW - timedelta(hours=1),
            from_state="eligible", to_state="exited",
        )
        events = [activated, exited, opened]
    elif violation == "bad_resume":
        resumed = event(
            "resume", event_id="illegal-resume",
            from_state="unavailable", to_state="eligible",
        )
        events = [resumed, opened]
    else:
        activated = event(
            "activate", event_id="forged-activation",
            from_state="eligible", to_state="eligible",
        )
        events = [activated, opened]
    with pytest.raises(ValueError, match="lifecycle|StateChart"):
        _attested_current_assignment(
            Ledger(events, seed_lifecycle=False),
            actor_id=ACTOR, owner_id=OWNER,
        )


def test_actor_work_replay_reads_global_event_snapshot_once():
    """P2: growing history must not repeatedly materialize global events."""
    class CountedLedger(Ledger):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.count = 0

        def events(self):
            self.count += 1
            return super().events()

    sequence = []
    for step in range(20):
        source = event("assign", event_id=f"start-{step}",
                       assigned_work_id=f"work:{step}")
        closed = event("complete", event_id=f"done-{step}",
                       completed_work_id=f"work:{step}")
        sequence.extend([source, closed])
    ledger = CountedLedger(sequence)
    assert _attested_current_assignment(
        ledger, actor_id=ACTOR, owner_id=OWNER,
    ) is None
    assert ledger.count == 1



@pytest.mark.parametrize("corruption", (
    "missing_activation_command", "wrong_action", "wrong_owner",
    "wrong_due_at", "wrong_correlation", "injected_payload",
    "duplicate_activation_descendant",
))
def test_activation_needs_exact_causal_command_before_work(corruption):
    activation = event(
        "activate", event_id="real-activation",
        at=NOW - timedelta(hours=2),
    )
    source = event(
        "assign", event_id="original-work", at=NOW,
        assigned_work_id=WORK,
    )
    original = issued_command_for(activation)
    assert original is not None
    if corruption == "missing_activation_command":
        cmd = None
    elif corruption == "wrong_action":
        cmd = replace(original, name="resume")
    elif corruption == "wrong_owner":
        cmd = replace(original, owner_id="org:forged")
    elif corruption == "wrong_due_at":
        cmd = replace(original, due_at=NOW)
    elif corruption == "wrong_correlation":
        cmd = replace(original, correlation_id="forged")
    elif corruption == "injected_payload":
        cmd = replace(original, payload={**original.payload, "extra": True})
    else:
        cmd = original
    history = [activation, source]
    if corruption == "duplicate_activation_descendant":
        history.append(replace(
            activation, event_id="duplicate-activation",
        ))
    ledger = Ledger(history, seed_lifecycle=False)
    saved_get_command = ledger.get_command
    def get_command(command_id):
        if command_id == activation.causation_id:
            return cmd
        return saved_get_command(command_id)
    ledger.get_command = get_command
    with pytest.raises(ValueError, match="attested actor lifecycle causal Command"):
        _attested_current_assignment(
            ledger, actor_id=ACTOR, owner_id=OWNER,
        )


def test_extra_non_work_event_cannot_share_cancellation_command():
    command, source, closed = valid_replay()
    ghost = replace(
        closed, event_id="ghost-descendant", name="arbitrary.event",
        payload={"forged": True},
    )
    ledger = Ledger([source, closed, ghost], command, source)
    with pytest.raises(ValueError, match="unique append-only event"):
        _attested_current_assignment(
            ledger, actor_id=ACTOR, owner_id=OWNER,
        )

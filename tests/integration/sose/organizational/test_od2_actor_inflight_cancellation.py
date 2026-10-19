"""#462 RED: explicit, paid actor-local in-flight cancel before exit.

This does not authorize cancellation of a business-domain ScheduledWork, nor
release an authoritative PhysicalReservation. It covers the actor's own
assigned_work_id projection with causal Command/Event and durable restart.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import ACTOR_KIND, seed_actor, transition_actor
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


T0 = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
OWNER = "org:actors-cancel"
MODEL = "actor-cancel-v1"


def _append_real_legacy_work_event(uow, original, actor_id, *, action, at, payload):
    """Materialize a valid pre-attestation Command and immutable event pair.

    Do not forge legacy history by recycling a modern or activation Command:
    source attribution must remain independently testable across restart.
    """
    cause = "legacy:" + action + ":" + at.isoformat()
    command_id = deterministic_id(
        "od2-actor-command", OWNER, actor_id, cause,
    )
    uow.save_command(Command(
        command_id=command_id, name=action, entity_type=ACTOR_KIND,
        entity_id=actor_id, due_at=at, issued_at=at, owner_id=OWNER,
        causation_id=None,
        correlation_id=deterministic_id("od2-actor-flow", OWNER, actor_id),
        payload={"external_cause_id": cause},
    ))
    uow.append_event(replace(
        original,
        event_id="historical:" + cause,
        occurred_at=at,
        causation_id=command_id,
        correlation_id=deterministic_id("od2-actor-flow", OWNER, actor_id),
        payload=payload,
    ))


def _transition(store, actor_id, action, hour, *, work_id=None, cost_us=0,
                cause_id=None, owner_id=OWNER):
    return transition_actor(
        store, owner_id=owner_id, actor_id=actor_id, action=action,
        cause_id=cause_id or ("actor:" + action + ":" + str(hour)),
        at=T0 + timedelta(hours=hour), model_spec_hash=MODEL, config_epoch=1,
        work_id=work_id, cost_us=cost_us,
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_paid_cancellation_frees_actor_assignment_before_exit_with_restart(adapter, tmp_path):
    path = tmp_path / "actor-cancel.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="cancelled", model_spec_hash=MODEL,
            config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:critical")
        with pytest.raises(Exception):
            _transition(store, actor_id, "exit", 3)
        assert store.entity(ACTOR_KIND, actor_id).attributes["assigned_work_id"] == "work:critical"
        before = tuple(store.events())
        cancelled = _transition(
            store, actor_id, "cancel_assigned", 3,
            work_id="work:critical", cost_us=750000,
        )
        assert cancelled.state == "eligible"
        assert cancelled.attributes["assigned_work_id"] is None
        assert cancelled.attributes["paid_transition_us"] == 750000
        assert cancelled.attributes["cancelled_work_ids"] == ["work:critical"]
        assert cancelled.attributes["time_us"]["active"] == 3600 * 1000000
        added = [e for e in store.events() if e.event_id not in {p.event_id for p in before}]
        assert len(added) == 1
        event = added[0]
        assert event.payload["trigger"] == "cancel_assigned"
        assert event.owner_id == OWNER
        assert store.command(event.causation_id).name == "cancel_assigned"
        assert store.command(event.causation_id).payload["external_cause_id"] == "actor:cancel_assigned:3"
        assert store.command(event.causation_id).payload["cancelled_work_id"] == "work:critical"
        assert store.command(event.causation_id).payload["charged_cost_us"] == 750000
        assert event.payload["cancelled_work_id"] == "work:critical"
        assert event.payload["charged_cost_us"] == 750000
        assignment = next(e for e in before if e.payload.get("trigger") == "assign")
        assert assignment.payload["assigned_work_id"] == "work:critical"
        assert event.payload["source_assignment_event_id"] == assignment.event_id
        _transition(store, actor_id, "exit", 4)
        assert store.entity(ACTOR_KIND, actor_id).state == "exited"
        before = tuple(store.events())
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id="work:critical", cost_us=750000)
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            before = tuple(recovered.events())
            _transition(recovered, actor_id, "cancel_assigned", 3,
                        work_id="work:critical", cost_us=750000)
            assert tuple(recovered.events()) == before
            subject = recovered.entity(ACTOR_KIND, actor_id)
            assert subject.state == "exited"
            assert subject.attributes["assigned_work_id"] is None
            assert subject.attributes["cancelled_work_ids"] == ["work:critical"]


@pytest.mark.parametrize("work_id,cost_us,match", [
    (None, 750000, "matching assigned work"),
    ("other", 750000, "matching assigned work"),
    ("work:critical", 0, "paid"),
])
def test_actor_cannot_cancel_without_fenced_work_id_and_paid_cost(work_id, cost_us, match):
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="negative",
                          model_spec_hash=MODEL, config_epoch=1, at=T0)
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:critical")
    before_actor = store.entity(ACTOR_KIND, actor_id)
    before_events = tuple(store.events())
    with pytest.raises(ValueError, match=match):
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id=work_id, cost_us=cost_us)
    assert store.entity(ACTOR_KIND, actor_id) == before_actor
    assert tuple(store.events()) == before_events
    assert store.entity(ACTOR_KIND, actor_id).attributes["paid_transition_us"] == 0


def test_actor_cancel_rejects_foreign_owner_and_cause_reuse_without_mutation():
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="owner-fence",
                          model_spec_hash=MODEL, config_epoch=1, at=T0)
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:critical")
    before = tuple(store.events())
    with pytest.raises(PermissionError, match="another operational owner"):
        _transition(store, actor_id, "cancel_assigned", 3,
                    owner_id="org:forged", work_id="work:critical", cost_us=1)
    assert tuple(store.events()) == before
    _transition(store, actor_id, "cancel_assigned", 3,
                work_id="work:critical", cost_us=750000)
    committed = tuple(store.events())
    with pytest.raises(ValueError, match="different action"):
        _transition(store, actor_id, "complete", 3, work_id="work:critical",
                    cause_id="actor:cancel_assigned:3")
    with pytest.raises(ValueError, match="different action"):
        _transition(store, actor_id, "cancel_assigned", 3, work_id="work:critical",
                    cost_us=1, cause_id="actor:cancel_assigned:3")
    assert tuple(store.events()) == committed



def test_actor_cancellation_rejects_forged_projection_without_real_assignment():
    """P1: a mutable actor work label must not invent authoritative work."""
    store = MemoryPersistence()
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="tampered-work",
        model_spec_hash=MODEL, config_epoch=1, at=T0,
    )
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:A")
    immutable = [e for e in store.events() if e.payload.get("trigger") == "assign"]
    assert len(immutable) == 1
    assert immutable[0].payload["assigned_work_id"] == "work:A"
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.attributes["assigned_work_id"] = "work:B"
        uow.save_entity(actor)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="attested|original assignment"):
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id="work:B", cost_us=1)
    # A forged completion must not silently close the old immutable work.
    with pytest.raises(ValueError, match="attested original assignment"):
        _transition(store, actor_id, "complete", 3, work_id="work:B")
    with pytest.raises(ValueError, match="matching assigned work"):
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id="work:A", cost_us=1)
    assert tuple(store.events()) == before
    assert store.entity(ACTOR_KIND, actor_id).state == "active"


@pytest.mark.parametrize("tamper", (
    "delete_command", "rewrite_command", "rewrite_issued_at",
    "rewrite_correlation", "rewrite_causation", "rewrite_tick",
    "rewrite_payload_extra",
))
def test_sqlite_replayed_cancellation_requires_immutable_cost_and_real_command(
    tmp_path, tamper,
):
    """P1: append-only cost/work evidence survives; mutable Command is checked."""
    path = tmp_path / ("cancel-tamper-" + tamper + ".sqlite")
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(store, owner_id=OWNER, actor_key="tamper-" + tamper,
                              model_spec_hash=MODEL, config_epoch=1, at=T0)
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:A")
        _transition(store, actor_id, "cancel_assigned", 3,
                    work_id="work:A", cost_us=750000)
        event = next(e for e in store.events()
                     if e.payload.get("trigger") == "cancel_assigned")
        immutable_event_id = event.event_id
        command_id = event.causation_id
        assert event.payload["charged_cost_us"] == 750000
        assert event.payload["cancelled_work_id"] == "work:A"
        with store.transaction(owner_id=OWNER) as uow:
            if tamper == "delete_command":
                uow.delete_command(command_id)
            else:
                old = uow.get_command(command_id)
                if tamper == "rewrite_command":
                    rewritten = replace(old, payload={
                        **old.payload, "charged_cost_us": 1,
                    })
                elif tamper == "rewrite_issued_at":
                    rewritten = replace(old, issued_at=old.issued_at + timedelta(seconds=1))
                elif tamper == "rewrite_correlation":
                    rewritten = replace(old, correlation_id="forged-correlation")
                elif tamper == "rewrite_causation":
                    rewritten = replace(old, causation_id="forged-event")
                elif tamper == "rewrite_tick":
                    rewritten = replace(old, tick=17)
                else:
                    rewritten = replace(old, payload={
                        **old.payload, "forged_extra": True,
                    })
                uow.save_command(rewritten)
    with SQLitePersistence(path) as recovered:
        before = tuple(recovered.events())
        assert any(e.event_id == immutable_event_id
                   and e.payload["charged_cost_us"] == 750000 for e in before)
        with pytest.raises(ValueError, match="cancellation.*evidence"):
            _transition(recovered, actor_id, "cancel_assigned", 3,
                        work_id="work:A", cost_us=750000)
        assert tuple(recovered.events()) == before



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_corrupted_projection_cannot_create_overlapping_durable_work(adapter, tmp_path):
    """P1: mutable eligible/idle projection cannot hide an open work:A source."""
    path = tmp_path / "overlap.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="overlap",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:A")
        first = next(event for event in store.events()
                     if event.payload.get("trigger") == "assign")
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.state = "eligible"
            actor.attributes["assigned_work_id"] = None
            uow.save_entity(actor)
        before = tuple(store.events())
        with pytest.raises(ValueError, match="attested assignment remains active"):
            _transition(store, actor_id, "assign", 3, work_id="work:B")
        assert tuple(store.events()) == before
        # Even a second tampering attempt making work:B the active projection
        # cannot turn it into a real source assignment.
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.state = "active"
            actor.attributes["assigned_work_id"] = "work:B"
            uow.save_entity(actor)
        with pytest.raises(ValueError, match="attested original assignment"):
            _transition(store, actor_id, "cancel_assigned", 4,
                        work_id="work:B", cost_us=1)
        assert tuple(store.events()) == before
        assert first.payload["assigned_work_id"] == "work:A"
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            old = tuple(recovered.events())
            with pytest.raises(ValueError, match="attested original assignment"):
                _transition(recovered, actor_id, "cancel_assigned", 4,
                            work_id="work:B", cost_us=1)
            assert tuple(recovered.events()) == old


def test_attested_history_rejects_ever_overlapping_assignments_even_if_last_is_valid():
    """P1 forged history: final assignment work:B does not hide unclosed work:A."""
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="forged-overlap",
                          model_spec_hash=MODEL, config_epoch=1, at=T0)
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:A")
    first = next(event for event in store.events()
                 if event.payload.get("trigger") == "assign")
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.state = "active"
        actor.attributes["assigned_work_id"] = "work:B"
        uow.save_entity(actor)
        uow.append_event(replace(
            first, event_id="forged-second-assignment",
            occurred_at=T0 + timedelta(hours=3),
            payload={**first.payload, "assigned_work_id": "work:B"},
        ))
    before = tuple(store.events())
    with pytest.raises(ValueError, match="attested assignments overlap"):
        _transition(store, actor_id, "cancel_assigned", 4,
                    work_id="work:B", cost_us=1)
    assert tuple(store.events()) == before





@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_corrupted_owner_cannot_filter_out_previous_attested_assignment(
    adapter, tmp_path,
):
    """P1: owner drift may not hide outstanding work in another owner ledger."""
    path = tmp_path / "owner-drift.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="owner-drift",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:A")
        original = tuple(store.events())
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.attributes["owner_id"] = "org:forged"
            actor.state = "eligible"
            actor.attributes["assigned_work_id"] = None
            uow.save_entity(actor)
        with pytest.raises((PermissionError, ValueError), match="attested.*owner|owner.*history"):
            _transition(
                store, actor_id, "assign", 3,
                owner_id="org:forged", work_id="work:B",
            )
        assert tuple(store.events()) == original
        with store.transaction(owner_id="org:forged") as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.state = "active"
            actor.attributes["assigned_work_id"] = "work:B"
            uow.save_entity(actor)
        with pytest.raises((PermissionError, ValueError), match="attested.*owner|owner.*history"):
            _transition(
                store, actor_id, "cancel_assigned", 4,
                owner_id="org:forged", work_id="work:B", cost_us=1,
            )
        assert tuple(store.events()) == original
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            with pytest.raises((PermissionError, ValueError), match="attested.*owner|owner.*history"):
                _transition(
                    reopened, actor_id, "cancel_assigned", 4,
                    owner_id="org:forged", work_id="work:B", cost_us=1,
                )
            assert tuple(reopened.events()) == original



@pytest.mark.parametrize("completed_work_id", (None, "work:B"))
def test_attested_assignment_does_not_accept_unattested_or_wrong_completion(
    completed_work_id,
):
    """P1: old-shaped completion cannot close modern append-only work:A."""
    store = MemoryPersistence()
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="forged-completion-" + str(completed_work_id),
        model_spec_hash=MODEL, config_epoch=1, at=T0,
    )
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:A")
    assignment = next(e for e in store.events() if e.payload.get("trigger") == "assign")
    malformed = {
        **assignment.payload,
        "trigger": "complete",
        "from_state": "active",
        "to_state": "eligible",
    }
    malformed.pop("assigned_work_id")
    if completed_work_id is not None:
        malformed["completed_work_id"] = completed_work_id
    with store.transaction(owner_id=OWNER) as uow:
        uow.append_event(replace(
            assignment, event_id="forged-completion-" + str(completed_work_id),
            occurred_at=T0 + timedelta(hours=3), payload=malformed,
        ))
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.state = "eligible"
        actor.attributes["assigned_work_id"] = None
        uow.save_entity(actor)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="attested work completion identity mismatch"):
        _transition(store, actor_id, "assign", 4, work_id="work:B")
    assert tuple(store.events()) == before


def test_closed_pre_attestation_assign_and_complete_pair_is_accepted():
    """Legacy missing work fields are allowed only when BOTH events predate IDs."""
    store = MemoryPersistence()
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="legacy-pair",
        model_spec_hash=MODEL, config_epoch=1, at=T0,
    )
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:A")
    _transition(store, actor_id, "complete", 3, work_id="work:A")
    original = [e for e in store.events()
                if e.payload.get("trigger") in {"assign", "complete"}]
    assert len(original) == 2
    old_assign, old_complete = original
    old_assign_data = dict(old_assign.payload)
    old_assign_data.pop("assigned_work_id")
    old_complete_data = dict(old_complete.payload)
    old_complete_data.pop("completed_work_id")
    with store.transaction(owner_id=OWNER) as uow:
        _append_real_legacy_work_event(
            uow, old_assign, actor_id, action="assign",
            at=T0 + timedelta(hours=4), payload=old_assign_data,
        )
        _append_real_legacy_work_event(
            uow, old_complete, actor_id, action="complete",
            at=T0 + timedelta(hours=5), payload=old_complete_data,
        )
    assigned = _transition(store, actor_id, "assign", 6, work_id="work:new")
    assert assigned.state == "active"
    assert assigned.attributes["assigned_work_id"] == "work:new"
    assert [e.payload["assigned_work_id"] for e in store.events()
            if e.payload.get("trigger") == "assign"
            and "assigned_work_id" in e.payload] == ["work:A", "work:new"]



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_upgrade_can_close_pre_attestation_inflight_work_without_poisoning_ledger(
    adapter, tmp_path,
):
    """Real legacy open work has no immutable work ID: preserve its old closing shape."""
    path = tmp_path / "pre-attestation-inflight.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="legacy-active",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        activated = next(
            e for e in store.events() if e.payload.get("trigger") == "activate"
        )
        # Simulate the persisted pre-upgrade actor and its append-only assign:
        # no assigned_work_id was emitted by the older StateChart adapter.
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.state = "active"
            actor.attributes["assigned_work_id"] = "work:legacy"
            actor.attributes["last_at"] = (T0 + timedelta(hours=2)).isoformat()
            uow.save_entity(actor)
            _append_real_legacy_work_event(
                uow, activated, actor_id, action="assign",
                at=T0 + timedelta(hours=2),
                payload={**activated.payload,
                         "trigger": "assign", "from_state": "eligible",
                         "to_state": "active"},
            )
        closed = _transition(
            store, actor_id, "complete", 3, work_id="work:legacy"
        )
        assert closed.state == "eligible"
        completion = next(e for e in store.events()
                          if e.payload.get("trigger") == "complete")
        assert "completed_work_id" not in completion.payload
        assigned = _transition(
            store, actor_id, "assign", 4, work_id="work:new"
        )
        assert assigned.attributes["assigned_work_id"] == "work:new"
        assert sum(e.payload.get("trigger") == "assign"
                   for e in store.events()) == 2
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert reopened.entity(ACTOR_KIND, actor_id).attributes[
                "assigned_work_id"
            ] == "work:new"



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("cost_marker", ("missing", 0, True, "750000", 750000))
def test_forged_cancel_without_paid_causal_authority_cannot_release_work(
    adapter, cost_marker, tmp_path,
):
    """Append-only malformed closure + mutable idle projection cannot free work:A.

    Even a positive forged cost is insufficient without the correctly
    causally linked, independently verified cancellation Command.
    """
    path = tmp_path / "forged-unpaid-close.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="cost-" + str(cost_marker),
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:A")
        original = next(e for e in store.events()
                        if e.payload.get("trigger") == "assign")
        closed_payload = {
            **original.payload,
            "trigger": "cancel_assigned",
            "cancelled_work_id": "work:A",
            "source_assignment_event_id": original.event_id,
        }
        closed_payload.pop("assigned_work_id", None)
        if cost_marker != "missing":
            closed_payload["charged_cost_us"] = cost_marker
        with store.transaction(owner_id=OWNER) as uow:
            uow.append_event(replace(
                original, event_id="forged-paid-close",
                occurred_at=T0 + timedelta(hours=3),
                payload=closed_payload,
            ))
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.state = "eligible"
            actor.attributes["assigned_work_id"] = None
            uow.save_entity(actor)
        immutable = tuple(store.events())
        with pytest.raises(ValueError, match="paid cost|causal|cancellation"):
            _transition(store, actor_id, "assign", 4, work_id="work:B")
        assert tuple(store.events()) == immutable
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            before = tuple(reopened.events())
            with pytest.raises(ValueError, match="paid cost|causal|cancellation"):
                _transition(reopened, actor_id, "assign", 4, work_id="work:B")
            assert tuple(reopened.events()) == before



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("forgery", ("assignment", "completion"))
def test_reused_unrelated_command_cannot_attest_work_across_restart(
    adapter, forgery, tmp_path,
):
    """P1: copy an event's causation from activate/assign to forge work."""
    path = tmp_path / ("forged-command-" + forgery + ".sqlite")
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="forged-" + forgery,
            model_spec_hash=MODEL, config_epoch=1, at=T0,
        )
        _transition(store, actor_id, "activate", 1)
        if forgery == "assignment":
            original = next(e for e in store.events()
                            if e.payload.get("trigger") == "activate")
            event_at = T0 + timedelta(hours=2)
            fake = replace(
                original, event_id="forged-assignment",
                occurred_at=event_at,
                payload={**original.payload,
                         "trigger": "assign",
                         "assigned_work_id": "work:forged"},
            )
        else:
            _transition(store, actor_id, "assign", 2, work_id="work:A")
            original = next(e for e in store.events()
                            if e.payload.get("trigger") == "assign")
            event_at = T0 + timedelta(hours=3)
            fake = replace(
                original, event_id="forged-completion",
                occurred_at=event_at,
                payload={**original.payload,
                         "trigger": "complete",
                         "completed_work_id": "work:A"},
            )
        with store.transaction(owner_id=OWNER) as uow:
            uow.append_event(fake)
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.state = "active" if forgery == "assignment" else "eligible"
            actor.attributes["assigned_work_id"] = (
                "work:forged" if forgery == "assignment" else None
            )
            uow.save_entity(actor)
        before = tuple(store.events())
        with pytest.raises(ValueError, match="attested work transition causal Command"):
            if forgery == "assignment":
                _transition(store, actor_id, "cancel_assigned", 4,
                            work_id="work:forged", cost_us=1)
            else:
                _transition(store, actor_id, "assign", 4, work_id="work:B")
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            before = tuple(recovered.events())
            with pytest.raises(ValueError, match="attested work transition causal Command"):
                if forgery == "assignment":
                    _transition(recovered, actor_id, "cancel_assigned", 4,
                                work_id="work:forged", cost_us=1)
                else:
                    _transition(recovered, actor_id, "assign", 4, work_id="work:B")
            assert tuple(recovered.events()) == before


def test_modern_assignment_and_completion_are_both_bound_to_original_command_work():
    store = MemoryPersistence()
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="modern-work-command",
        model_spec_hash=MODEL, config_epoch=1, at=T0,
    )
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:A")
    _transition(store, actor_id, "complete", 3, work_id="work:A")
    transitions = [
        e for e in store.events()
        if e.payload.get("trigger") in {"assign", "complete"}
    ]
    assert [e.payload.get("assigned_work_id") for e in transitions] == ["work:A", None]
    assert [e.payload.get("completed_work_id") for e in transitions] == [None, "work:A"]
    assert store.command(transitions[0].causation_id).payload["assigned_work_id"] == "work:A"
    assert store.command(transitions[1].causation_id).payload["completed_work_id"] == "work:A"
    _transition(store, actor_id, "assign", 4, work_id="work:B")
    assert store.entity(ACTOR_KIND, actor_id).attributes["assigned_work_id"] == "work:B"

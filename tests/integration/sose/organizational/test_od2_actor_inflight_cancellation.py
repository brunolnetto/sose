"""#462 RED: explicit, paid actor-local in-flight cancel before exit.

This does not authorize cancellation of a business-domain ScheduledWork, nor
release an authoritative PhysicalReservation. It covers the actor's own
assigned_work_id projection with causal Command/Event and durable restart.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from sose.organizational.od2_actor import ACTOR_KIND, seed_actor, transition_actor
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


T0 = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
OWNER = "org:actors-cancel"
MODEL = "actor-cancel-v1"


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


def test_closed_historical_assignment_without_work_id_allows_new_assignment():
    """Retain bounded compatibility for older completed actor event projections."""
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="legacy-complete",
                          model_spec_hash=MODEL, config_epoch=1, at=T0)
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="work:old")
    _transition(store, actor_id, "complete", 3, work_id="work:old")
    before = tuple(store.events())
    with store.transaction(owner_id=OWNER) as uow:
        for old in before:
            if old.payload.get("trigger") in {"assign", "complete"}:
                updated_payload = dict(old.payload)
                updated_payload.pop("assigned_work_id", None)
                updated_payload.pop("completed_work_id", None)
                # Replay a historical ledger whose original schema predates
                # append-only actor work IDs. Does not modify frozen files.
                # Persistence append order remains unchanged.
                new_event = replace(old, event_id=old.event_id + "-historical",
                                    payload=updated_payload)
                uow.append_event(new_event)
    # The appended historical simulation conflicts with modern events,
    # and must fail closed rather than silently certifying new work.
    old_events = tuple(store.events())
    with pytest.raises(ValueError, match="attested"):
        _transition(store, actor_id, "assign", 4, work_id="work:new")
    assert tuple(store.events()) == old_events

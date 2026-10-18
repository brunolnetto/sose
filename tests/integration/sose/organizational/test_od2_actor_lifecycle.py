"""TDD #462: authoritative actor-time, transitions, ownership and restart.

This is a bounded OD-2 lifecycle slice, NOT a physical work-slot capacity or
causal effect of agency on OD-5a/OD-5b.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.od2_actor import (
    ACTOR_KIND, ACTOR_STATES, ActorAvailabilityCalendar, ActorAvailabilityInterval, seed_actor, transition_actor,
)
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


T0 = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)
OWNER = "org:research-A"
MODEL = "bounded-actor-model-hash"


def _seed(store, *, owner=OWNER, key="actor-1"):
    return seed_actor(
        store, owner_id=owner, actor_key=key,
        model_spec_hash=MODEL, config_epoch=1, at=T0,
    )


def _transition(store, actor_id, name, hour, **kwargs):
    return transition_actor(
        store, owner_id=OWNER, actor_id=actor_id,
        action=name, cause_id="observation:" + name + ":" + str(hour),
        at=T0 + timedelta(hours=hour), model_spec_hash=MODEL,
        config_epoch=1, **kwargs,
    )


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_actor_statechart_accounts_distinct_idleness_and_development_without_leak(
    adapter, tmp_path,
):
    path = tmp_path / "actor-ledger.sqlite"
    if adapter == "sqlite":
        store = SQLitePersistence(path)
    else:
        store = MemoryPersistence()
    try:
        actor_id = _seed(store)
        peer_id = _seed(store, key="actor-2")
        assert actor_id != peer_id
        _transition(store, actor_id, "activate", 1)
        _transition(store, actor_id, "assign", 2, work_id="work:1")
        _transition(store, actor_id, "complete", 3, work_id="work:1")
        _transition(store, actor_id, "begin_development", 4, cost_us=4500000)
        _transition(store, actor_id, "finish_development", 6)
        _transition(store, actor_id, "exit", 7)
        subject = store.entity(ACTOR_KIND, actor_id)
        assert subject is not None and subject.state == "exited"
        assert subject.attributes["assigned_work_id"] is None
        assert subject.attributes["time_us"] == {
            "admitted": 3600 * 1000000,
            "eligible": 3 * 3600 * 1000000,
            "active": 3600 * 1000000,
            "unavailable": 0,
            "development": 2 * 3600 * 1000000,
            "exited": 0,
        }
        assert set(subject.attributes["time_us"]) == set(ACTOR_STATES)
        assert sum(subject.attributes["time_us"].values()) == 7 * 3600 * 1000000
        assert subject.attributes["paid_transition_us"] == 4500000
        assert store.entity(ACTOR_KIND, peer_id).state == "admitted"
        events = [e for e in store.events() if e.entity_id == actor_id]
        assert len(events) == 6
        assert len({e.event_id for e in events}) == 6
        assert all(e.owner_id == OWNER for e in events)
        assert all(e.causation_id for e in events)
        commands = [store.command(e.causation_id) for e in events]
        assert all(c is not None and c.owner_id == OWNER for c in commands)
        assert {e.payload["trigger"] for e in events} == {
            "activate", "assign", "complete",
            "begin_development", "finish_development", "exit",
        }
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert recovered.entity(ACTOR_KIND, actor_id) == subject
            assert len([e for e in recovered.events() if e.entity_id == actor_id]) == 6
            before = recovered.events()
            _transition(recovered, actor_id, "exit", 7)
            assert recovered.events() == before


def test_actor_fenced_replay_illegal_exit_and_stale_epoch_leave_no_causal_writes():
    store = MemoryPersistence()
    actor = _seed(store)
    _transition(store, actor, "activate", 1)
    assigned = _transition(store, actor, "assign", 2, work_id="work:1")
    events = store.events()
    with pytest.raises(ValueError, match="different action"):
        transition_actor(
            store, owner_id=OWNER, actor_id=actor, action="complete",
            cause_id="observation:assign:2", at=T0 + timedelta(hours=2),
            model_spec_hash=MODEL, config_epoch=1, work_id="work:1",
        )
    with pytest.raises(ValueError, match="stale or unadmitted"):
        transition_actor(
            store, owner_id=OWNER, actor_id=actor, action="complete",
            cause_id="new-cause", at=T0 + timedelta(hours=3),
            model_spec_hash=MODEL, config_epoch=2, work_id="work:1",
        )
    with pytest.raises(PermissionError, match="another operational owner"):
        transition_actor(
            store, owner_id="org:research-B", actor_id=actor, action="complete",
            cause_id="foreign-cause", at=T0 + timedelta(hours=3),
            model_spec_hash=MODEL, config_epoch=1, work_id="work:1",
        )
    with pytest.raises(Exception):
        _transition(store, actor, "exit", 3)
    with pytest.raises(ValueError, match="matching assigned work"):
        _transition(store, actor, "complete", 3, work_id="other")
    with pytest.raises(ValueError, match="regressed"):
        _transition(store, actor, "complete", 1, work_id="work:1")
    assert store.entity(ACTOR_KIND, actor) == assigned
    assert store.events() == events
    _transition(store, actor, "complete", 3, work_id="work:1")
    _transition(store, actor, "make_unavailable", 4)
    with pytest.raises(Exception):
        _transition(store, actor, "assign", 5, work_id="work:2")
    _transition(store, actor, "resume", 5)
    _transition(store, actor, "exit", 6)
    assert store.entity(ACTOR_KIND, actor).state == "exited"
    assert store.entity(ACTOR_KIND, actor).attributes["time_us"]["unavailable"] == (
        3600 * 1000000
    )


def test_actor_replacement_requires_exited_same_owner_and_preserves_predecessor():
    store = MemoryPersistence()
    actor = _seed(store)
    with pytest.raises(ValueError, match="prior exited actor"):
        seed_actor(
            store, owner_id=OWNER, actor_key="replacement",
            model_spec_hash=MODEL, config_epoch=1,
            at=T0 + timedelta(hours=1), replaces_actor_id=actor,
        )
    _transition(store, actor, "activate", 1)
    _transition(store, actor, "exit", 2)
    successor = seed_actor(
        store, owner_id=OWNER, actor_key="replacement",
        model_spec_hash=MODEL, config_epoch=1,
        at=T0 + timedelta(hours=2), replaces_actor_id=actor,
    )
    assert successor != actor
    assert store.entity(ACTOR_KIND, successor).state == "admitted"
    assert store.entity(ACTOR_KIND, successor).attributes["replaces_actor_id"] == actor
    assert store.entity(ACTOR_KIND, actor).state == "exited"
    assert store.entity(ACTOR_KIND, actor).attributes["replaced_by_actor_id"] == successor
    for other_key in ("alternative-successor", "third-successor"):
        with pytest.raises(ValueError, match="predecessor already replaced"):
            seed_actor(
                store, owner_id=OWNER, actor_key=other_key,
                model_spec_hash=MODEL, config_epoch=1,
                at=T0 + timedelta(hours=2),
                replaces_actor_id=actor,
            )
    with pytest.raises(ValueError, match="already exists"):
        seed_actor(
            store, owner_id=OWNER, actor_key="replacement",
            model_spec_hash=MODEL, config_epoch=1,
            at=T0 + timedelta(hours=3), replaces_actor_id=actor,
        )


@pytest.mark.parametrize(
    "at,cost_us,epoch",
    [
        (datetime(2026, 10, 17, 9), 0, 1),
        (T0, -1, 1),
        (T0, True, 1),
        (T0, 0, True),
    ],
)
def test_actor_rejects_ambiguous_clock_cost_or_epoch(at, cost_us, epoch):
    store = MemoryPersistence()
    actor = _seed(store)
    with pytest.raises(ValueError):
        transition_actor(
            store, owner_id=OWNER, actor_id=actor,
            action="activate", cause_id="admit:1", at=at,
            model_spec_hash=MODEL, config_epoch=epoch, cost_us=cost_us,
        )
    assert store.entity(ACTOR_KIND, actor).state == "admitted"
    assert not store.events()


@pytest.mark.parametrize(
    "epoch,model",
    [(0, MODEL), (2, "silently-replaced-model"), (1, "other-model")],
)
def test_actor_replacement_cannot_silently_rebind_epoch_or_model(epoch, model):
    store = MemoryPersistence()
    prior = _seed(store)
    _transition(store, prior, "activate", 1)
    _transition(store, prior, "exit", 2)
    before = store.entity(ACTOR_KIND, prior)
    with pytest.raises(ValueError, match="separate admission"):
        seed_actor(
            store, owner_id=OWNER, actor_key="new-actor",
            model_spec_hash=model, config_epoch=epoch,
            at=T0 + timedelta(hours=2), replaces_actor_id=prior,
        )
    assert store.entity(ACTOR_KIND, prior) == before
    assert store.entity(ACTOR_KIND, prior).attributes["replaced_by_actor_id"] is None
    # A rejected proposal has not consumed the only legal successor identity.
    successor = seed_actor(
        store, owner_id=OWNER, actor_key="new-actor",
        model_spec_hash=MODEL, config_epoch=1,
        at=T0 + timedelta(hours=2), replaces_actor_id=prior,
    )
    assert store.entity(ACTOR_KIND, prior).attributes["replaced_by_actor_id"] == successor



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_actor_calendar_fences_assignment_at_exact_window_boundary(adapter, tmp_path):
    """Source-declared schedule controls claim start; it never grants physical capacity."""
    path = tmp_path / "od2-calendar.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        calendar = ActorAvailabilityCalendar(
            version=1, config_epoch=1,
            intervals=(ActorAvailabilityInterval(
                starts_at=T0 + timedelta(hours=2),
                ends_at=T0 + timedelta(hours=3),
            ), ActorAvailabilityInterval(
                starts_at=T0 + timedelta(hours=5),
                ends_at=T0 + timedelta(hours=6),
            )),
        )
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="with-calendar",
            model_spec_hash=MODEL, config_epoch=1, at=T0,
            availability_calendar=calendar,
        )
        _transition(store, actor_id, "activate", 1)
        source = store.entity(ACTOR_KIND, actor_id)
        assert source.attributes["availability_calendar"]["config_epoch"] == 1
        assert source.attributes["availability_calendar"]["source_digest"] == calendar.digest()
        events_before = tuple(store.events())
        for hour in (1.5, 3):
            with pytest.raises(ValueError, match="calendar.*assignment"):
                _transition(store, actor_id, "assign", hour, work_id="work:calendar")
            assert tuple(store.events()) == events_before
            assert store.entity(ACTOR_KIND, actor_id) == source
        _transition(store, actor_id, "assign", 2, work_id="work:calendar")
        # Completion can be outside the start eligibility interval. This is
        # NOT proof of continued service or a live ResourceReservation.
        _transition(store, actor_id, "complete", 4, work_id="work:calendar")
        assert store.entity(ACTOR_KIND, actor_id).state == "eligible"
        assert len(store.events()) == len(events_before) + 2
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert reopened.entity(ACTOR_KIND, actor_id).attributes["availability_calendar"]["source_digest"] == calendar.digest()
            old = reopened.events()
            _transition(reopened, actor_id, "complete", 4, work_id="work:calendar")
            assert reopened.events() == old
            with pytest.raises(ValueError, match="calendar.*assignment"):
                _transition(reopened, actor_id, "assign", 6, work_id="reopened:bad")
            assert reopened.events() == old
            _transition(reopened, actor_id, "assign", 5, work_id="reopened:valid")
            _transition(reopened, actor_id, "complete", 7, work_id="reopened:valid")
            assert reopened.entity(ACTOR_KIND, actor_id).state == "eligible"


@pytest.mark.parametrize("case", ("overlap", "zero", "epoch", "unordered"))
def test_actor_calendar_requires_positive_ordered_intervals_and_epoch(case):
    first = ActorAvailabilityInterval(
        T0 + timedelta(hours=2), T0 + timedelta(hours=3),
    )
    second = ActorAvailabilityInterval(
        T0 + timedelta(hours=4), T0 + timedelta(hours=5),
    )
    if case == "zero":
        with pytest.raises(ValueError, match="positive"):
            ActorAvailabilityInterval(T0, T0)
    elif case == "overlap":
        with pytest.raises(ValueError, match="overlap"):
            ActorAvailabilityCalendar(1, 1, (
                first, ActorAvailabilityInterval(
                    T0 + timedelta(hours=2, minutes=30),
                    T0 + timedelta(hours=4),
                ),
            ))
    elif case == "unordered":
        with pytest.raises(ValueError, match="ordered"):
            ActorAvailabilityCalendar(1, 1, (second, first))
    else:
        store = MemoryPersistence()
        calendar = ActorAvailabilityCalendar(1, 2, (first,))
        with pytest.raises(ValueError, match="epoch"):
            seed_actor(store, owner_id=OWNER, actor_key="epoch",
                       model_spec_hash=MODEL, config_epoch=1, at=T0,
                       availability_calendar=calendar)
        assert tuple(store.events()) == ()


def test_actor_calendar_tampering_refuses_new_causal_commands():
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=1, intervals=(ActorAvailabilityInterval(
            T0 + timedelta(hours=1), T0 + timedelta(hours=4),
        ),),
    )
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="tampering",
        model_spec_hash=MODEL, config_epoch=1, at=T0,
        availability_calendar=calendar,
    )
    _transition(store, actor_id, "activate", 1)
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.attributes["availability_calendar"]["intervals"][0]["end"] = (
            T0 + timedelta(days=2)
        ).isoformat()
        uow.save_entity(actor)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="calendar.*digest"):
        _transition(store, actor_id, "assign", 2, work_id="tampered")
    assert tuple(store.events()) == before



@pytest.mark.parametrize("mutation", ("remove_calendar", "recompute_digest"))
def test_actor_calendar_enrollment_source_is_independent_of_mutable_projection(mutation):
    """P1: dropping or rehashing the actor blob never forges enrollment."""
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(1, 1, (ActorAvailabilityInterval(
        T0 + timedelta(hours=2), T0 + timedelta(hours=3),
    ),))
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="independent-enrollment-" + mutation,
        model_spec_hash=MODEL, config_epoch=1, at=T0,
        availability_calendar=calendar,
    )
    from sose.core.identity import deterministic_id
    enrolled_id = deterministic_id("od2-actor-calendar-admitted", actor_id)
    assert any(event.event_id == enrolled_id for event in store.events())
    _transition(store, actor_id, "activate", 1)
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        if mutation == "remove_calendar":
            del actor.attributes["availability_calendar"]
        else:
            actor.attributes["availability_calendar"]["intervals"][0]["end"] = (
                T0 + timedelta(days=3)
            ).isoformat()
            actor.attributes["availability_calendar"]["source_digest"] = (
                ActorAvailabilityCalendar(
                    1, 1, (ActorAvailabilityInterval(
                        T0 + timedelta(hours=2), T0 + timedelta(days=3),
                    ),),
                ).digest()
            )
        uow.save_entity(actor)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="actor calendar"):
        _transition(store, actor_id, "assign", 5, work_id="ghost-work")
    assert tuple(store.events()) == before


@pytest.mark.parametrize("epoch", (True, 1.0))
def test_actor_calendar_rejects_noninteger_corrupted_durable_epoch(epoch):
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(1, 1, (ActorAvailabilityInterval(
        T0 + timedelta(hours=2), T0 + timedelta(hours=3),
    ),))
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="epoch-corrupt:" + repr(epoch),
        model_spec_hash=MODEL, config_epoch=1, at=T0,
        availability_calendar=calendar,
    )
    _transition(store, actor_id, "activate", 1)
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.attributes["config_epoch"] = epoch
        uow.save_entity(actor)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="config_epoch"):
        _transition(store, actor_id, "assign", 2, work_id="should-not-exist")
    assert tuple(store.events()) == before


def test_actor_same_cause_replay_validates_enrolled_source_first():
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(1, 1, (ActorAvailabilityInterval(
        T0 + timedelta(hours=2), T0 + timedelta(hours=3),
    ),))
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="replay-tamper",
        model_spec_hash=MODEL, config_epoch=1, at=T0,
        availability_calendar=calendar,
    )
    _transition(store, actor_id, "activate", 1)
    _transition(store, actor_id, "assign", 2, work_id="original")
    before = tuple(store.events())
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.attributes["availability_calendar"]["source_digest"] = "f" * 64
        uow.save_entity(actor)
    with pytest.raises(ValueError, match="actor calendar"):
        _transition(store, actor_id, "assign", 2, work_id="original")
    assert tuple(store.events()) == before


def test_actor_owner_and_calendar_deletion_cannot_hide_enrollment_marker():
    """P1 regression: lookup must use immutable actor ID, not mutable owner."""
    store = MemoryPersistence()
    cal = ActorAvailabilityCalendar(
        1, 1, (ActorAvailabilityInterval(
            T0 + timedelta(hours=2), T0 + timedelta(hours=3),
        ),),
    )
    actor_id = seed_actor(
        store, owner_id=OWNER, actor_key="forged-owner",
        model_spec_hash=MODEL, config_epoch=1, at=T0,
        availability_calendar=cal,
    )
    _transition(store, actor_id, "activate", 1)
    forged_owner = "org:forged"
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        actor.attributes["owner_id"] = forged_owner
        del actor.attributes["availability_calendar"]
        uow.save_entity(actor)
    original_events = tuple(store.events())
    with pytest.raises(ValueError, match="actor calendar"):
        transition_actor(
            store, owner_id=forged_owner, actor_id=actor_id,
            action="assign", cause_id="forged:assign",
            at=T0 + timedelta(hours=5), work_id="ghost",
            model_spec_hash=MODEL, config_epoch=1,
        )
    assert tuple(store.events()) == original_events
    assert store.entity(ACTOR_KIND, actor_id).attributes["assigned_work_id"] is None

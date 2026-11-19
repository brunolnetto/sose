"""#462: incremental opt-in actor-time ledger (tracked_time=True) edge cases.

The guard verifies only the ledger head by keyed reads; these tests pin its
store-size independence, replay, owner, replacement, calendar and
state-set evolution behavior. Untracked actors must stay unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.organizational import od2_actor_time_ledger as ledger
from sose.organizational.od2_actor import (
    ACTOR_KIND, ActorAvailabilityCalendar, ActorAvailabilityInterval, seed_actor,
    transition_actor,
)
from sose.persistence.memory import MemoryPersistence, MemoryUnitOfWork
from sose.persistence.sqlite import SQLitePersistence


T0 = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)
OWNER = "org:research-A"
OTHER = "org:research-B"
MODEL = "bounded-actor-model-hash"


def _seed(store, *, owner=OWNER, key="actor-1", tracked_time=True, **kwargs):
    return seed_actor(
        store, owner_id=owner, actor_key=key, model_spec_hash=MODEL,
        config_epoch=1, at=kwargs.pop("at", T0), tracked_time=tracked_time, **kwargs,
    )


def _transition(store, actor_id, name, hour, *, owner=OWNER, **kwargs):
    return transition_actor(
        store, owner_id=owner, actor_id=actor_id, action=name,
        cause_id="observation:" + name + ":" + str(hour),
        at=T0 + timedelta(hours=hour), model_spec_hash=MODEL,
        config_epoch=1, **kwargs,
    )


def _ledger_events(store, actor_id):
    return [
        event for event in store.events()
        if event.name in (ledger.STEP_EVENT, ledger.ORIGIN_EVENT)
        and event.entity_id == actor_id
    ]


def _tamper(store, actor_id, mutate, *, owner=OWNER):
    with store.transaction(owner_id=owner) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        mutate(actor)
        uow.save_entity(actor)


def _foreign_events(store, count):
    with store.transaction(owner_id=OTHER) as uow:
        for index in range(count):
            uow.append_event(DomainEvent(
                event_id=deterministic_id("foreign-noise", index),
                name="foreign.noise", entity_type="noise", entity_id=str(index),
                occurred_at=T0, owner_id=OTHER, payload={"index": index},
            ))


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_tracked_transition_never_scans_full_event_log(adapter, tmp_path, monkeypatch):
    """Finding: per-transition cost must not grow with other owners' events."""
    store = (
        MemoryPersistence() if adapter == "memory"
        else SQLitePersistence(tmp_path / "ledger-scan.sqlite")
    )
    try:
        _foreign_events(store, 1500)
        actor_id = _seed(store)
        calls = []
        original = MemoryUnitOfWork.events

        def spy(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(MemoryUnitOfWork, "events", spy)
        _transition(store, actor_id, "activate", 1, cost_us=3)
        _transition(store, actor_id, "assign", 2, work_id="work:scan", cost_us=7)
        _transition(store, actor_id, "complete", 3, work_id="work:scan")
        _transition(store, actor_id, "begin_development", 4, cost_us=11)
        _transition(store, actor_id, "finish_development", 5)
        result = _transition(store, actor_id, "exit", 6)
        assert calls == []
        assert result.attributes["paid_transition_us"] == 21
        monkeypatch.undo()
        steps = [e for e in _ledger_events(store, actor_id) if e.name == ledger.STEP_EVENT]
        assert [e.payload["seq"] for e in steps] == [1, 2, 3, 4, 5, 6]
        assert steps[-1].payload["after"]["state"] == "exited"
    finally:
        if adapter == "sqlite":
            store.close()


def test_untracked_actor_writes_no_ledger_records_and_skips_guard():
    store = MemoryPersistence()
    actor_id = _seed(store, tracked_time=False)
    _transition(store, actor_id, "activate", 1, cost_us=2)
    _transition(store, actor_id, "make_unavailable", 2)
    assert _ledger_events(store, actor_id) == []
    assert "tracked_time_version" not in store.entity(ACTOR_KIND, actor_id).attributes
    # Untracked projections are unattested: tampering is not detected here.
    _tamper(store, actor_id, lambda a: a.attributes["time_us"].update(eligible=1))
    _transition(store, actor_id, "resume", 3)


def test_tracked_time_flag_must_be_boolean():
    with pytest.raises(TypeError, match="explicit boolean"):
        _seed(MemoryPersistence(), tracked_time=1)


@pytest.mark.parametrize("mutation", ["inflate", "paid", "work", "cause"])
def test_tracked_same_cause_replay_runs_guard_before_returning(mutation):
    store = MemoryPersistence()
    actor_id = _seed(store)
    _transition(store, actor_id, "activate", 1, cost_us=4)
    assigned = _transition(store, actor_id, "assign", 2, work_id="work:r", cost_us=1)
    assert _transition(store, actor_id, "assign", 2, work_id="work:r", cost_us=1) == assigned

    def mutate(actor):
        attrs = actor.attributes
        if mutation == "inflate":
            attrs["time_us"]["eligible"] += 5
        elif mutation == "paid":
            attrs["paid_transition_us"] += 1
        elif mutation == "work":
            attrs["assigned_work_id"] = "work:forged"
        else:
            attrs["applied_causes"]["observation:activate:1"][3] = 0

    _tamper(store, actor_id, mutate)
    with pytest.raises(ValueError, match="OD-2 actor-time"):
        _transition(store, actor_id, "assign", 2, work_id="work:r", cost_us=1)


def test_reverting_projection_to_earlier_verified_snapshot_is_detected():
    """Incremental head check must still catch a whole-projection rollback."""
    store = MemoryPersistence()
    actor_id = _seed(store)
    _transition(store, actor_id, "activate", 1)
    earlier = store.entity(ACTOR_KIND, actor_id)
    _transition(store, actor_id, "begin_development", 2, cost_us=50)
    events = store.events()

    def rollback(actor):
        actor.state = earlier.state
        actor.attributes.clear()
        actor.attributes.update(earlier.attributes)

    _tamper(store, actor_id, rollback)
    with pytest.raises(ValueError, match="OD-2 actor-time.*truncated"):
        _transition(store, actor_id, "make_unavailable", 3)
    assert store.events() == events


def test_state_set_evolution_compares_only_present_buckets():
    """Adding a lifecycle state later must not brick stored tracked actors."""
    store = MemoryPersistence()
    actor_id = _seed(store)
    _transition(store, actor_id, "activate", 1)

    def legacy_state_set(actor):
        # A projection written before "exited"/"development" existed, plus a
        # zero bucket for a state this code version does not know.
        del actor.attributes["time_us"]["exited"]
        del actor.attributes["time_us"]["development"]
        actor.attributes["time_us"]["future_state"] = 0

    _tamper(store, actor_id, legacy_state_set)
    _transition(store, actor_id, "begin_development", 2, cost_us=3)
    done = _transition(store, actor_id, "finish_development", 4)
    assert done.attributes["time_us"]["development"] == 2 * 3600 * 1000000
    _tamper(store, actor_id, lambda a: a.attributes["time_us"].update(
        future_state=a.attributes["time_us"].pop("development"),
    ))
    with pytest.raises(ValueError, match="OD-2 actor-time"):
        _transition(store, actor_id, "exit", 5)


@pytest.mark.parametrize("value", [-1, 1.5, "1", None])
def test_malformed_bucket_values_fail_closed(value):
    store = MemoryPersistence()
    actor_id = _seed(store)
    _transition(store, actor_id, "activate", 1)
    _tamper(store, actor_id, lambda a: a.attributes["time_us"].update(exited=value))
    with pytest.raises(ValueError, match="OD-2 actor-time"):
        _transition(store, actor_id, "exit", 2)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_tracked_actors_are_owner_isolated_for_same_key(adapter, tmp_path):
    store = (
        MemoryPersistence() if adapter == "memory"
        else SQLitePersistence(tmp_path / "ledger-owners.sqlite")
    )
    try:
        first = _seed(store, owner=OWNER, key="shared-key")
        second = _seed(store, owner=OTHER, key="shared-key")
        assert first != second
        _transition(store, first, "activate", 1, cost_us=5)
        _transition(store, second, "activate", 2, owner=OTHER, cost_us=9)
        _transition(store, first, "assign", 3, work_id="work:a")
        _transition(store, second, "begin_development", 3, owner=OTHER, cost_us=1)
        with pytest.raises(PermissionError, match="another operational owner"):
            _transition(store, second, "finish_development", 4, owner=OWNER)
        a = _transition(store, first, "complete", 4, work_id="work:a")
        b = _transition(store, second, "finish_development", 4, owner=OTHER)
        assert a.attributes["paid_transition_us"] == 5
        assert b.attributes["paid_transition_us"] == 10
        assert {e.owner_id for e in _ledger_events(store, first)} == {OWNER}
        assert {e.owner_id for e in _ledger_events(store, second)} == {OTHER}
        # Corrupting one owner's actor does not affect the other owner.
        _tamper(store, first, lambda x: x.attributes.update(paid_transition_us=0))
        with pytest.raises(ValueError, match="OD-2 actor-time"):
            _transition(store, first, "exit", 5)
        assert _transition(store, second, "exit", 5, owner=OTHER).state == "exited"
    finally:
        if adapter == "sqlite":
            store.close()


def test_tracked_replacement_of_untracked_legacy_actor():
    store = MemoryPersistence()
    legacy = _seed(store, key="legacy", tracked_time=False)
    _transition(store, legacy, "activate", 1)
    _transition(store, legacy, "exit", 2)
    successor = _seed(
        store, key="successor", at=T0 + timedelta(hours=2), replaces_actor_id=legacy,
    )
    origin = store.events()[-1]
    assert origin.name == ledger.ORIGIN_EVENT
    assert origin.payload["replaces_actor_id"] == legacy
    _transition(store, successor, "activate", 3, cost_us=2)
    _transition(store, successor, "assign", 4, work_id="work:succ")
    assert store.entity(ACTOR_KIND, legacy).attributes["replaced_by_actor_id"] == successor
    assert _ledger_events(store, legacy) == []
    _tamper(store, successor, lambda a: a.attributes.update(replaces_actor_id=None))
    with pytest.raises(ValueError, match="OD-2 actor-time enrollment"):
        _transition(store, successor, "complete", 5, work_id="work:succ")


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_tracked_actor_with_availability_calendar(adapter, tmp_path):
    path = tmp_path / "ledger-calendar.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    calendar = ActorAvailabilityCalendar(1, 1, (ActorAvailabilityInterval(
        T0 + timedelta(hours=2), T0 + timedelta(hours=4),
    ),))
    try:
        actor_id = _seed(store, key="calendar", availability_calendar=calendar)
        _transition(store, actor_id, "activate", 1, cost_us=1)
        with pytest.raises(ValueError, match="calendar.*assignment"):
            _transition(store, actor_id, "assign", 1, work_id="work:cal")
        _transition(store, actor_id, "assign", 3, work_id="work:cal", cost_us=2)
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        store = SQLitePersistence(path)
    try:
        done = _transition(store, actor_id, "complete", 5, work_id="work:cal")
        assert done.attributes["time_us"]["active"] == 2 * 3600 * 1000000
        assert done.attributes["paid_transition_us"] == 3
        _tamper(store, actor_id, lambda a: a.attributes["time_us"].update(active=1))
        with pytest.raises(ValueError, match="OD-2 actor-time"):
            _transition(store, actor_id, "exit", 6)
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("version", ["remove", 2, True])
def test_tracked_marker_cannot_be_removed_while_origin_event_remains(version):
    store = MemoryPersistence()
    actor_id = _seed(store)
    _transition(store, actor_id, "activate", 1)
    before = store.events()

    def strip(actor):
        if version == "remove":
            del actor.attributes["tracked_time_version"]
        else:
            actor.attributes["tracked_time_version"] = version

    _tamper(store, actor_id, strip)
    with pytest.raises(ValueError, match="OD-2 actor-time enrollment"):
        _transition(store, actor_id, "make_unavailable", 2)
    with pytest.raises(ValueError, match="OD-2 actor-time enrollment"):
        _transition(store, actor_id, "activate", 1)
    assert store.events() == before


def test_marker_without_origin_event_is_rejected():
    """Claiming tracked status on an untracked actor cannot fabricate history."""
    store = MemoryPersistence()
    actor_id = _seed(store, tracked_time=False)
    _tamper(store, actor_id, lambda a: a.attributes.update(tracked_time_version=1))
    with pytest.raises(ValueError, match="OD-2 actor-time enrollment source missing"):
        _transition(store, actor_id, "activate", 1)


def _squat(store, event_id, *, name="foreign.squat"):
    """A foreign owner appends an arbitrary event at a predictable ledger ID."""
    with store.transaction(owner_id=OTHER) as uow:
        uow.append_event(DomainEvent(
            event_id=event_id, name=name, entity_type="noise", entity_id=event_id,
            occurred_at=T0, owner_id=OTHER, payload={},
        ))


def _store(adapter, tmp_path, name):
    if adapter == "memory":
        return MemoryPersistence()
    return SQLitePersistence(tmp_path / (name + ".sqlite"))


def _close(store):
    if isinstance(store, SQLitePersistence):
        store.close()


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_foreign_event_at_origin_id_cannot_disable_untracked_actor(adapter, tmp_path):
    """Review finding: untracked actors must behave exactly as on main."""
    store = _store(adapter, tmp_path, "squat-origin")
    try:
        actor_id = _seed(store, tracked_time=False)
        _squat(store, ledger.origin_event_id(actor_id))
        _transition(store, actor_id, "activate", 1, cost_us=2)
        assert _transition(store, actor_id, "activate", 1, cost_us=2).state == "eligible"
        assert _transition(store, actor_id, "exit", 2).state == "exited"
    finally:
        _close(store)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_genuine_origin_still_enforces_after_marker_strip(adapter, tmp_path):
    store = _store(adapter, tmp_path, "genuine-origin")
    try:
        actor_id = _seed(store)
        _transition(store, actor_id, "activate", 1)
        _tamper(store, actor_id, lambda a: a.attributes.pop("tracked_time_version"))
        with pytest.raises(ValueError, match="OD-2 actor-time enrollment"):
            _transition(store, actor_id, "exit", 2)
    finally:
        _close(store)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_tracked_seed_fails_clearly_when_origin_id_is_taken(adapter, tmp_path):
    store = _store(adapter, tmp_path, "seed-collision")
    try:
        actor_id = deterministic_id("od2-actor", OWNER, "actor-1")
        _squat(store, ledger.origin_event_id(actor_id))
        before = store.events()
        with pytest.raises(ValueError, match="origin ID already committed"):
            _seed(store)
        assert store.entity(ACTOR_KIND, actor_id) is None
        assert store.events() == before
    finally:
        _close(store)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_foreign_event_at_next_step_id_is_reported_as_occupied(adapter, tmp_path):
    store = _store(adapter, tmp_path, "step-collision")
    try:
        actor_id = _seed(store)
        _transition(store, actor_id, "activate", 1)
        _squat(store, ledger.step_event_id(actor_id, 2))
        before = store.events()
        with pytest.raises(ValueError, match="ledger ID occupied by a foreign event"):
            _transition(store, actor_id, "exit", 2)
        assert store.events() == before
    finally:
        _close(store)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_strip_marker_and_swap_owner_cannot_bypass_guard(adapter, tmp_path):
    """Delta-review falsifier: 'genuine origin' must not read editable owner."""
    store = _store(adapter, tmp_path, "owner-swap")
    try:
        actor_id = _seed(store)
        _transition(store, actor_id, "activate", 1, cost_us=7)

        def hijack(actor):
            actor.attributes.pop("tracked_time_version")
            actor.attributes["owner_id"] = OTHER
            actor.attributes["paid_transition_us"] = 0

        _tamper(store, actor_id, hijack)
        before = store.events()
        with pytest.raises(ValueError, match="OD-2 actor-time enrollment"):
            _transition(store, actor_id, "make_unavailable", 2, owner=OTHER)
        assert store.events() == before
    finally:
        _close(store)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_forged_origin_from_foreign_writer_locks_untracked_actor_fail_closed(
    adapter, tmp_path,
):
    """Residual #545 limit: append_event does not bind owner_id to the writer.

    A fully forged origin locks an untracked actor; it must fail closed and
    write nothing (it cannot fabricate tracked history).
    """
    store = _store(adapter, tmp_path, "forged-origin")
    try:
        actor_id = _seed(store, tracked_time=False)
        actor = store.entity(ACTOR_KIND, actor_id)
        with store.transaction(owner_id=OTHER) as uow:
            uow.append_event(DomainEvent(
                event_id=ledger.origin_event_id(actor_id), name=ledger.ORIGIN_EVENT,
                entity_type=ACTOR_KIND, entity_id=actor_id, owner_id=OWNER,
                occurred_at=actor.created_at, payload={
                    "owner_id": OWNER, "model_spec_hash": MODEL, "config_epoch": 1,
                    "replaces_actor_id": None, "tracked_time_version": 1,
                    "actor_key": "actor-1",
                },
            ))
        before = store.events()
        with pytest.raises(ValueError, match="OD-2 actor-time enrollment"):
            _transition(store, actor_id, "activate", 1)
        assert store.events() == before
        assert store.entity(ACTOR_KIND, actor_id).state == "admitted"
    finally:
        _close(store)

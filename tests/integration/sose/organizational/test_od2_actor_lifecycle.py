"""TDD #462: authoritative actor-time, transitions, ownership and restart.

This is a bounded OD-2 lifecycle slice, NOT a physical work-slot capacity or
causal effect of agency on OD-5a/OD-5b.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.od2_actor import (
    ACTOR_KIND, ACTOR_STATES, seed_actor, transition_actor,
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

"""OD-2 negative falsifiers for immutable calendar and actor causal gates.

These scenarios use the real in-memory UoW, not mocked persistence. Invalid
admission, calendar tampering, and replay must not introduce durable effects.
"""
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import (
    ACTOR_KIND,
    ActorAvailabilityCalendar,
    ActorAvailabilityInterval,
    seed_actor,
    transition_actor,
)
from sose.persistence.memory import MemoryPersistence

T0 = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
OWNER = "org:od2-tail"
MODEL = "od2-tail-model"


def enrollment(store, *, actor_key="person", **changes):
    args = dict(
        owner_id=OWNER, actor_key=actor_key, model_spec_hash=MODEL,
        config_epoch=1, at=T0,
    )
    args.update(changes)
    return seed_actor(store, **args)


def action(store, actor_id, name="activate", **changes):
    args = dict(
        owner_id=OWNER, actor_id=actor_id, action=name,
        cause_id="cause:" + name, at=T0 + timedelta(hours=1),
        model_spec_hash=MODEL, config_epoch=1,
    )
    args.update(changes)
    return transition_actor(store, **args)


@pytest.mark.parametrize("version,epoch,windows,exc,pattern", [
    (True, 1, "valid", ValueError, "version"),
    (2, 1, "valid", ValueError, "version"),
    (1, True, "valid", ValueError, "epoch"),
    (1, -1, "valid", ValueError, "epoch"),
    (1, 1, [], ValueError, "nonempty intervals"),
    (1, 1, (), ValueError, "nonempty intervals"),
    (1, 1, ("not-an-interval",), TypeError, "typed immutable"),
])
def test_calendar_rejects_noncanonical_schema(version, epoch, windows, exc, pattern):
    if windows == "valid":
        windows = (ActorAvailabilityInterval(T0, T0 + timedelta(hours=2)),)
    with pytest.raises(exc, match=pattern):
        ActorAvailabilityCalendar(version=version, config_epoch=epoch,
                                  intervals=windows)


@pytest.mark.parametrize("key,value,exc,pattern", [
    ("owner_id", "  ", ValueError, "owner_id must be nonempty"),
    ("actor_key", None, ValueError, "actor_key must be nonempty"),
    ("model_spec_hash", "", ValueError, "model_spec_hash must be nonempty"),
    ("config_epoch", True, ValueError, "config_epoch"),
    ("config_epoch", -1, ValueError, "config_epoch"),
    ("availability_calendar", "not-a-calendar", TypeError, "ActorAvailabilityCalendar"),
    ("replaces_actor_id", deterministic_id("od2-actor", OWNER, "person"),
     ValueError, "cannot replace itself"),
])
def test_actor_admission_rejects_invalid_source_without_persisting(key, value, exc, pattern):
    store = MemoryPersistence()
    with pytest.raises(exc, match=pattern):
        enrollment(store, **{key: value})
    assert tuple(store.events()) == ()
    assert store.entity(ACTOR_KIND, deterministic_id("od2-actor", OWNER, "person")) is None


def test_actor_admission_rejects_calendar_epoch_drift():
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(1, 2, (
        ActorAvailabilityInterval(T0, T0 + timedelta(hours=2)),
    ))
    with pytest.raises(ValueError, match="calendar epoch"):
        enrollment(store, availability_calendar=calendar)
    assert tuple(store.events()) == ()


@pytest.mark.parametrize("mutation", [
    "wrong_projection_shape", "wrong_intervals_shape",
    "wrong_interval_item", "malformed_interval", "wrong_digest",
])
def test_calendar_corrupted_projection_fails_closed(mutation):
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(1, 1, (
        ActorAvailabilityInterval(T0, T0 + timedelta(hours=3)),
    ))
    actor_id = enrollment(store, availability_calendar=calendar)
    before_events = tuple(store.events())
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        if mutation == "wrong_projection_shape":
            actor.attributes["availability_calendar"] = "untrusted"
        elif mutation == "wrong_intervals_shape":
            actor.attributes["availability_calendar"]["intervals"] = "untrusted"
        elif mutation == "wrong_interval_item":
            actor.attributes["availability_calendar"]["intervals"] = ["untrusted"]
        elif mutation == "malformed_interval":
            actor.attributes["availability_calendar"]["intervals"] = [{"start": T0.isoformat()}]
        else:
            actor.attributes["availability_calendar"]["source_digest"] = "forged"
        uow.save_entity(actor)
    with pytest.raises(ValueError, match="calendar.*(shape|source|digest|enrollment)"):
        action(store, actor_id)
    assert tuple(store.events()) == before_events


@pytest.mark.parametrize("changes,exc,pattern", [
    ({"actor_id": "unknown"}, KeyError, "unknown organizational actor"),
    ({"model_spec_hash": "forged"}, ValueError, "model_spec_hash"),
    ({"owner_id": "org:foreign"}, PermissionError, "another operational owner"),
    ({"config_epoch": 2}, ValueError, "config_epoch"),
    ({"cost_us": True}, ValueError, "nonnegative microseconds"),
    ({"cost_us": -1}, ValueError, "nonnegative microseconds"),
    ({"work_id": "work:unassigned"}, ValueError, "work identity"),
])
def test_actor_lifecycle_invalid_request_makes_no_durable_effect(changes, exc, pattern):
    store = MemoryPersistence()
    actor_id = enrollment(store)
    before_events = tuple(store.events())
    before_actor = store.entity(ACTOR_KIND, actor_id)
    with pytest.raises(exc, match=pattern):
        action(store, actor_id, **changes)
    assert tuple(store.events()) == before_events
    assert store.entity(ACTOR_KIND, actor_id) == before_actor


def test_actor_development_cannot_be_free():
    store = MemoryPersistence()
    actor_id = enrollment(store)
    action(store, actor_id)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="development must have explicit paid cost"):
        action(store, actor_id, "begin_development", cause_id="development")
    assert tuple(store.events()) == before

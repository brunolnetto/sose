"""OD-2 offline legacy audit must NEVER invent admission provenance (#462)."""
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import (
    ACTOR_KIND, ActorAvailabilityCalendar, ActorAvailabilityInterval, seed_actor,
    transition_actor,
)
from sose.organizational.od2_legacy_audit import audit_legacy_actor_origins
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
OWNER, MODEL = "org:audit", "model:fixed"


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_legacy_calendar_and_markerless_actors_are_never_auto_admitted(adapter, tmp_path):
    path = tmp_path / "old-actor.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    try:
        bare = seed_actor(store, owner_id=OWNER, actor_key="calendar-free",
                          model_spec_hash=MODEL, config_epoch=3, at=T0)
        marker = seed_actor(store, owner_id=OWNER, actor_key="with-calendar",
                            model_spec_hash=MODEL, config_epoch=3, at=T0,
                            availability_calendar=calendar)
        transitions = tuple(store.events())
        initial = tuple(store.entities())
        evidence = audit_legacy_actor_origins(initial, transitions)
        assert {x.actor_id: x.classification for x in evidence} == {
            bare: "quarantined_unattested",
            marker: "legacy_calendar_evidence_only",
        }
        assert not any(item.admits_transition for item in evidence)
        assert tuple(store.entities()) == initial
        assert tuple(store.events()) == transitions
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert audit_legacy_actor_origins(
                recovered.entities(), recovered.events(),
            ) == evidence


@pytest.mark.parametrize("tamper", ("owner", "source_digest", "remove_source"))
def test_corrupted_legacy_projection_is_a_conflict_not_backfilled(tamper):
    store = MemoryPersistence()
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="corrupt:" + tamper,
                          model_spec_hash=MODEL, config_epoch=3, at=T0,
                          availability_calendar=calendar)
    with store.transaction(owner_id=OWNER) as uow:
        actor = uow.get_entity(ACTOR_KIND, actor_id)
        if tamper == "owner":
            actor.attributes["owner_id"] = "org:attacker"
        elif tamper == "source_digest":
            actor.attributes["availability_calendar"]["source_digest"] = "forged"
        else:
            actor.attributes.pop("availability_calendar")
        uow.save_entity(actor)
    original = store.events()
    result, = audit_legacy_actor_origins(store.entities(), original)
    assert result.classification == "conflicting_legacy_marker"
    assert not result.admits_transition
    assert store.events() == original


def test_legacy_fieldless_work_is_reported_without_making_up_id():
    store = MemoryPersistence()
    actor_id = seed_actor(store, owner_id=OWNER, actor_key="legacy-assignment",
                          model_spec_hash=MODEL, config_epoch=3, at=T0)
    transition_actor(
        store, owner_id=OWNER, actor_id=actor_id, action="activate",
        cause_id="activate", at=T0 + timedelta(hours=1),
        model_spec_hash=MODEL, config_epoch=3,
    )
    transition_actor(
        store, owner_id=OWNER, actor_id=actor_id, action="assign",
        cause_id="assign", work_id="real:work",
        at=T0 + timedelta(hours=2), model_spec_hash=MODEL, config_epoch=3,
    )
    result, = audit_legacy_actor_origins(store.entities(), store.events())
    assert result.classification == "quarantined_unattested"
    assert result.missing_work_id_assignments == 1
    assert not result.admits_transition

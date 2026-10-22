"""OD-2 offline legacy audit must NEVER invent admission provenance (#462)."""
from dataclasses import replace
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



@pytest.mark.parametrize("which", ("calendar_marker", "transition"))
@pytest.mark.parametrize("malformed", (None, [], "opaque"))
def test_malformed_persisted_actor_event_payload_is_reported_not_executed(
    which, malformed, tmp_path,
):
    """Malformed historical SQLite JSON must quarantine, never crash audit."""
    path = tmp_path / "invalid-historic-payload.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="invalid:" + which,
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        if which == "transition":
            transition_actor(
                store, owner_id=OWNER, actor_id=actor_id, action="activate",
                cause_id="activate", at=T0 + timedelta(hours=1),
                model_spec_hash=MODEL, config_epoch=3,
            )
        with store.transaction(owner_id=OWNER) as uow:
            all_events = uow._working.events
            index = 0 if which == "calendar_marker" else -1
            all_events[index] = replace(all_events[index], payload=malformed)
    with SQLitePersistence(path) as restored:
        result, = audit_legacy_actor_origins(restored.entities(), restored.events())
        assert result.classification == "conflicting_legacy_marker"
        assert "non-mapping" in result.reason
        assert not result.admits_transition



@pytest.mark.parametrize("bad_source", (T0, b"codec-supported", {1: "mixed", "x": 2}))
def test_sqlite_restored_legacy_calendar_source_non_json_value_is_quarantined(
    bad_source, tmp_path,
):
    """The tagged SOSE codec accepts values the calendar JSON digest cannot."""
    path = tmp_path / "non-json-calendar-source.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="invalid-json-source",
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            event = uow._working.events[0]
            payload = dict(event.payload)
            payload["calendar_source"] = {
                **payload["calendar_source"], "corruption": bad_source,
            }
            uow._working.events[0] = replace(event, payload=payload)
    with SQLitePersistence(path) as recovered:
        result, = audit_legacy_actor_origins(recovered.entities(), recovered.events())
        assert result.classification == "conflicting_legacy_marker"
        assert "cannot be hashed" in result.reason
        assert not result.admits_transition



@pytest.mark.parametrize("malformed_attributes", (None, [], "invalid", True))
def test_legacy_audit_quarantines_corrupt_actor_attribute_container(
    malformed_attributes, tmp_path,
):
    """A malformed restored actor must not abort the rest of a snapshot audit."""
    path = tmp_path / "legacy-broken-attributes.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        bad = seed_actor(
            store, owner_id=OWNER, actor_key="bad-attributes",
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        good = seed_actor(
            store, owner_id=OWNER, actor_key="intact-calendar",
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, bad)
            actor.attributes = malformed_attributes
            uow.save_entity(actor)
    with SQLitePersistence(path) as restored:
        result = {
            item.actor_id: item
            for item in audit_legacy_actor_origins(
                restored.entities(), restored.events(),
            )
        }
        assert result[bad].classification == "conflicting_legacy_marker"
        assert "attributes" in result[bad].reason
        assert result[good].classification == "legacy_calendar_evidence_only"
        assert not result[bad].admits_transition
        assert not result[good].admits_transition


@pytest.mark.parametrize("invalid", (
    "future_version", "wrong_epoch", "empty_intervals", "overlap",
))
def test_self_consistent_but_invalid_calendar_is_not_migration_evidence(
    invalid, tmp_path,
):
    """Matching digests never make an invalid calendar an admission proof."""
    from hashlib import sha256
    from json import dumps

    path = tmp_path / ("calendar-invalid-" + invalid + ".sqlite")
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="bad-calendar:" + invalid,
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            marker = uow._working.events[0]
            source = dict(marker.payload["calendar_source"])
            if invalid == "future_version":
                source["version"] = 99
            elif invalid == "wrong_epoch":
                source["config_epoch"] = 4
            elif invalid == "empty_intervals":
                source["intervals"] = []
            else:
                source["intervals"] = [
                    {"start": T0.isoformat(), "end": (T0 + timedelta(days=1)).isoformat()},
                    {"start": (T0 + timedelta(hours=1)).isoformat(),
                     "end": (T0 + timedelta(days=2)).isoformat()},
                ]
            digest = sha256(dumps(
                source, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
            uow._working.events[0] = replace(
                marker, payload={
                    **marker.payload,
                    "calendar_source": source,
                    "calendar_digest": digest,
                },
            )
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.attributes["availability_calendar"] = {
                **source, "source_digest": digest,
            }
            uow.save_entity(actor)

    with SQLitePersistence(path) as recovered:
        finding, = audit_legacy_actor_origins(
            recovered.entities(), recovered.events(),
        )
        assert finding.classification == "conflicting_legacy_marker"
        assert not finding.admits_transition


@pytest.mark.parametrize("unexpected_value", ("unexpected", None, {"nested": True}))
def test_sqlite_restored_calendar_projection_rejects_unexpected_fields(
    unexpected_value, tmp_path,
):
    """A matching source digest cannot authorize an extended projection schema."""
    path = tmp_path / "legacy-extra-calendar-projection.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="extra-calendar-projection",
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.attributes["availability_calendar"]["unexpected"] = unexpected_value
            uow.save_entity(actor)
    with SQLitePersistence(path) as restored:
        before_entities, before_events = restored.entities(), restored.events()
        finding, = audit_legacy_actor_origins(before_entities, before_events)
        assert finding.classification == "conflicting_legacy_marker"
        assert not finding.admits_transition
        assert restored.entities() == before_entities
        assert restored.events() == before_events


@pytest.mark.parametrize("field,bad_value", (
    ("owner_id", None), ("owner_id", ""), ("owner_id", 42),
    ("model_spec_hash", None), ("model_spec_hash", ""),
    ("model_spec_hash", 42),
))
def test_restored_missing_provenance_identity_fails_closed(field, bad_value, tmp_path):
    path = tmp_path / "invalid-legacy-provenance.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="invalid-provenance",
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.attributes[field] = bad_value
            uow.save_entity(actor)
            marker = uow._working.events[0]
            payload = dict(marker.payload)
            payload[field] = bad_value
            uow._working.events[0] = replace(marker, payload=payload)
    with SQLitePersistence(path) as restored:
        original_events = restored.events()
        original_entities = restored.entities()
        finding, = audit_legacy_actor_origins(original_entities, original_events)
        assert finding.classification == "conflicting_legacy_marker"
        assert not finding.admits_transition
        assert restored.events() == original_events
        assert restored.entities() == original_entities


@pytest.mark.parametrize("key,value", (
    ("version", True), ("version", 1.0),
    ("config_epoch", True), ("config_epoch", 3.0),
))
def test_restored_calendar_projection_rejects_numeric_type_confusion(
    key, value, tmp_path,
):
    """Python numeric equality cannot authenticate a malformed projection."""
    path = tmp_path / "legacy-projection-numeric-types.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=3,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="numeric-projection",
            model_spec_hash=MODEL, config_epoch=3, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            actor = uow.get_entity(ACTOR_KIND, actor_id)
            actor.attributes["availability_calendar"][key] = value
            uow.save_entity(actor)
    with SQLitePersistence(path) as restored:
        entities, events = restored.entities(), restored.events()
        finding, = audit_legacy_actor_origins(entities, events)
        assert finding.classification == "conflicting_legacy_marker"
        assert not finding.admits_transition
        assert restored.entities() == entities
        assert restored.events() == events


@pytest.mark.parametrize("bad_epoch,valid_epoch", ((True, 1), (3.0, 3)))
def test_restored_marker_epoch_type_confusion_is_conflicting(bad_epoch, valid_epoch, tmp_path):
    """An untrusted legacy marker must not exploit Python numeric equality."""
    path = tmp_path / "legacy-marker-epoch.sqlite"
    calendar = ActorAvailabilityCalendar(
        version=1, config_epoch=valid_epoch,
        intervals=(ActorAvailabilityInterval(T0, T0 + timedelta(days=1)),),
    )
    with SQLitePersistence(path) as store:
        actor_id = seed_actor(
            store, owner_id=OWNER, actor_key="marker-epoch",
            model_spec_hash=MODEL, config_epoch=valid_epoch, at=T0,
            availability_calendar=calendar,
        )
        with store.transaction(owner_id=OWNER) as uow:
            marker = uow._working.events[0]
            uow._working.events[0] = replace(
                marker, payload={**marker.payload, "config_epoch": bad_epoch},
            )
    with SQLitePersistence(path) as restored:
        before_entities, before_events = restored.entities(), restored.events()
        finding, = audit_legacy_actor_origins(before_entities, before_events)
        assert finding.classification == "conflicting_legacy_marker"
        assert not finding.admits_transition
        assert restored.entities() == before_entities
        assert restored.events() == before_events

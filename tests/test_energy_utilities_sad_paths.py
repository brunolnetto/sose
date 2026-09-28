import pytest

from sose.examples.energy_utilities.scenarios import ORIGIN
from sose.examples.energy_utilities.simulation import (
    build_runtime,
    record_meter_reading,
    report_outage,
    restore_outage,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    return persistence, entities, engine


def test_corrected_reading_is_new_immutable_occurrence():
    persistence, entities, engine = _runtime()

    base = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=10.0,
        quality="actual",
    )
    assert base is not None and base.state == "committed"

    correction = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=10.2,
        quality="corrected",
        correction_ordinal=1,
        supersedes_reading_id=base.id,
    )

    assert correction is not None and correction.state == "committed"
    assert correction.id != base.id
    assert correction.attributes["supersedes_reading_id"] == base.id
    persisted_base = persistence.entity("utility_meter_reading", base.id)
    assert persisted_base is not None
    assert persisted_base.attributes["quantity_kwh"] == 10.0


def test_reading_identity_rejects_conflicting_reobservation():
    persistence, entities, engine = _runtime()
    record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=10.0,
    )

    with pytest.raises(ValueError, match="different measurement"):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN,
            quantity_kwh=10.1,
        )


def test_correction_requires_prior_committed_reading():
    persistence, entities, engine = _runtime()

    with pytest.raises(RuntimeError, match="was not persisted"):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN,
            quantity_kwh=10.1,
            quality="corrected",
            correction_ordinal=1,
            supersedes_reading_id="missing-reading",
        )


def test_overlapping_outages_hold_service_interrupted_until_all_restore():
    persistence, entities, engine = _runtime()

    first = report_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="feeder-a",
    )
    second = report_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="transformer-b",
    )
    point = persistence.entity("utility_service_point", entities.service_point_id)
    assert point is not None and point.state == "interrupted"
    assert set(point.attributes["open_outage_keys"]) == {
        "feeder-a",
        "transformer-b",
    }

    assert restore_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="feeder-a",
    )
    point = persistence.entity("utility_service_point", entities.service_point_id)
    assert point is not None and point.state == "interrupted"
    assert point.attributes["open_outage_keys"] == ["transformer-b"]

    assert restore_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="transformer-b",
    )
    point = persistence.entity("utility_service_point", entities.service_point_id)
    assert point is not None and point.state == "energized"
    assert point.attributes["open_outage_keys"] == []
    assert persistence.entity("utility_outage", first.id).state == "restored"
    assert persistence.entity("utility_outage", second.id).state == "restored"


def test_terminal_outage_replay_does_not_interrupt_restored_service():
    persistence, entities, engine = _runtime()
    outage = report_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="feeder-replay",
    )
    assert restore_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="feeder-replay",
    )

    replay = report_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="feeder-replay",
    )

    point = persistence.entity("utility_service_point", entities.service_point_id)
    assert replay.id == outage.id
    assert replay.state == "restored"
    assert point is not None and point.state == "energized"
    assert point.attributes["open_outage_keys"] == []

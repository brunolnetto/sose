import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.energy_utilities.scenarios import ORIGIN
from sose.examples.energy_utilities.simulation import (
    build_runtime,
    cancel_demand_response,
    dr_event_id,
    dr_participation_id,
    record_meter_reading,
    report_outage,
    restore_outage,
    schedule_demand_response,
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


def test_correction_lineage_and_quality_must_agree():
    persistence, entities, engine = _runtime()
    base = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=5.0,
    )
    assert base is not None

    with pytest.raises(ValueError, match="requires correction lineage"):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN,
            quantity_kwh=5.1,
            quality="corrected",
        )

    with pytest.raises(ValueError, match="requires corrected quality"):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN,
            quantity_kwh=5.1,
            quality="actual",
            correction_ordinal=1,
            supersedes_reading_id=base.id,
        )


def test_demand_response_replay_reuses_original_window():
    persistence, entities, engine = _runtime()
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    event, _, start_at, end_at = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="dr-replay-window",
    )
    backend.run_until(ORIGIN + (start_at - ORIGIN) / 2)

    replay_event, _, replay_start, replay_end = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="dr-replay-window",
    )

    assert replay_event.id == event.id
    assert replay_start == start_at
    assert replay_end == end_at


def test_cancelling_demand_response_removes_pending_boundaries():
    persistence, entities, engine = _runtime()
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    event, participation, _, end_at = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="dr-cancel",
    )
    assert len(persistence.scheduled_work()) == 2

    assert cancel_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="dr-cancel",
    )
    assert persistence.entity("utility_dr_event", dr_event_id("dr-cancel")).state == "cancelled"
    assert persistence.scheduled_work() == ()

    backend.run_until(end_at)
    assert persistence.entity("utility_dr_event", event.id).state == "cancelled"
    assert persistence.entity(
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    ).id == participation.id

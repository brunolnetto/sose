from sose.backends.simpy import SimPyBackend
from sose.examples.energy_utilities.entities import MeterReading
from sose.examples.energy_utilities.scenarios import ORIGIN
from sose.examples.energy_utilities.simulation import (
    build_runtime,
    dr_event_id,
    dr_participation_id,
    reading_id,
    reconcile_demand_response,
    record_meter_reading,
    schedule_demand_response,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_demand_response_window_survives_runtime_rebuild():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    event, participation, start_at, end_at = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="dr-restart",
    )
    assert len(persistence.scheduled_work()) == 2

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(start_at)
    assert persistence.entity("utility_dr_event", event.id).state == "active"
    assert reconcile_demand_response(
        persistence,
        rebuilt.engine,
        entities=entities,
        event_key="dr-restart",
    )

    rebuilt.backend.run_until(end_at)
    assert reconcile_demand_response(
        persistence,
        rebuilt.engine,
        entities=entities,
        event_key="dr-restart",
    )
    assert persistence.entity("utility_dr_event", dr_event_id("dr-restart")).state == "completed"
    assert persistence.entity(
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    ).state == "completed"
    assert persistence.scheduled_work() == ()


def test_captured_meter_reading_resumes_commit_after_rebuild():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reading = engine.context.entities.create(
        MeterReading,
        key=("energy-reference", entities.meter_id, ORIGIN.isoformat(), 0),
        state="captured",
        attributes={
            "meter_id": entities.meter_id,
            "service_point_id": entities.service_point_id,
            "interval_end": ORIGIN.isoformat(),
            "quantity_kwh": 7.5,
            "quality": "actual",
            "correction_ordinal": 0,
            "supersedes_reading_id": None,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(reading)

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    recovered = record_meter_reading(
        persistence,
        rebuilt.engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=7.5,
    )

    assert recovered is not None
    assert recovered.id == reading_id(entities.meter_id, ORIGIN)
    assert recovered.state == "committed"

def test_persisted_reading_replay_ignores_new_ingestion_outage():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reading = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=8.25,
    )
    assert reading is not None and reading.state == "committed"

    _, unavailable_engine = build_runtime(
        persistence,
        scenarios=(
            __import__(
                "sose.examples.energy_utilities.scenarios",
                fromlist=["demand_response_communications_outage"],
            ).demand_response_communications_outage,
        ),
    )
    unavailable_engine.context.scenarios.set_attribute(
        "energy.metering.available",
        False,
    )
    replay = record_meter_reading(
        persistence,
        unavailable_engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=8.25,
    )

    assert replay is not None
    assert replay.id == reading.id
    assert replay.state == "committed"


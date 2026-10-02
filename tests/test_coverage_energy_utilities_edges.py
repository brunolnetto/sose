from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.energy_utilities.scenarios import ORIGIN
from sose.examples.energy_utilities.simulation import (
    EnergyEntities,
    _entity,
    build_runtime,
    cancel_demand_response,
    dr_event_id,
    dr_participation_id,
    opt_out_demand_response,
    reading_id,
    reconcile_demand_response,
    record_meter_reading,
    report_outage,
    restore_outage,
    schedule_demand_response,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(**seed_kwargs):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, **seed_kwargs)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def test_missing_energy_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "utility_meter", "missing")


@pytest.mark.parametrize(
    ("quantity", "quality", "ordinal", "supersedes", "message"),
    [
        (-1.0, "actual", 0, None, "finite and non-negative"),
        (1.0, "invalid", 0, None, "unsupported meter reading quality"),
        (1.0, "actual", -1, None, "correction_ordinal must be non-negative"),
        (1.0, "actual", 0, "prior", "base reading cannot supersede"),
    ],
)
def test_meter_reading_reachable_validation_edges(
    quantity, quality, ordinal, supersedes, message
):
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(ValueError, match=message):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN,
            quantity_kwh=quantity,
            quality=quality,
            correction_ordinal=ordinal,
            supersedes_reading_id=supersedes,
        )


def test_metering_outage_returns_none_without_creating_occurrence(monkeypatch):
    persistence, entities, engine, _ = _runtime()
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "energy.metering.available"
        else default,
    )

    result = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=1.0,
    )

    assert result is None
    assert persistence.entity(
        "utility_meter_reading",
        reading_id(entities.meter_id, ORIGIN),
    ) is None


def test_meter_reading_requires_active_meter():
    persistence, entities, engine, _ = _runtime()
    meter = persistence.entity("utility_meter", entities.meter_id)
    assert meter is not None
    meter.state = "retired"
    _save(persistence, meter)

    with pytest.raises(RuntimeError, match="requires active meter"):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN,
            quantity_kwh=1.0,
        )


def test_correction_requires_same_meter():
    persistence, entities, engine, _ = _runtime()
    assert entities.secondary_meter_id is not None
    base = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=1.0,
    )
    assert base is not None

    secondary_entities = EnergyEntities(
        service_point_id=entities.secondary_service_point_id,
        meter_id=entities.secondary_meter_id,
    )
    with pytest.raises(ValueError, match="same meter"):
        record_meter_reading(
            persistence,
            engine,
            entities=secondary_entities,
            interval_end=ORIGIN,
            quantity_kwh=2.0,
            quality="corrected",
            correction_ordinal=1,
            supersedes_reading_id=base.id,
        )


def test_correction_requires_same_interval():
    persistence, entities, engine, _ = _runtime()
    base = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=1.0,
    )
    assert base is not None

    with pytest.raises(ValueError, match="preserve interval_end"):
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=ORIGIN + timedelta(hours=1),
            quantity_kwh=2.0,
            quality="corrected",
            correction_ordinal=1,
            supersedes_reading_id=base.id,
        )


def test_outage_requires_nonempty_incident_key():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(ValueError, match="incident_key"):
        report_outage(
            persistence,
            engine,
            entities=entities,
            incident_key="",
        )


def test_restored_outage_replay_is_idempotent():
    persistence, entities, engine, _ = _runtime()
    report_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="outage-a",
    )
    assert restore_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="outage-a",
    )
    events_before = tuple(persistence.events())

    assert restore_outage(
        persistence,
        engine,
        entities=entities,
        incident_key="outage-a",
    )
    assert tuple(persistence.events()) == events_before


@pytest.mark.parametrize(
    ("event_key", "start_delay", "duration", "message"),
    [
        ("", timedelta(), timedelta(hours=1), "event_key"),
        ("x", timedelta(seconds=-1), timedelta(hours=1), "start_delay"),
        ("x", timedelta(), timedelta(), "duration"),
        ("x", timedelta(), timedelta(seconds=-1), "duration"),
    ],
)
def test_demand_response_schedule_validates_window(
    event_key, start_delay, duration, message
):
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match=message):
        schedule_demand_response(
            persistence,
            engine,
            backend,
            entities=entities,
            event_key=event_key,
            start_delay=start_delay,
            duration=duration,
        )


def test_demand_response_replay_rejects_target_population_change():
    persistence, entities, engine, backend = _runtime()
    assert entities.secondary_service_point_id is not None
    schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="population",
        target_service_point_ids=(entities.service_point_id,),
    )

    with pytest.raises(ValueError, match="target population cannot change"):
        schedule_demand_response(
            persistence,
            engine,
            backend,
            entities=entities,
            event_key="population",
            target_service_point_ids=(
                entities.service_point_id,
                entities.secondary_service_point_id,
            ),
        )


def test_cancelled_demand_response_is_idempotent():
    persistence, entities, engine, backend = _runtime()
    schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="cancelled",
    )
    assert cancel_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="cancelled",
    )
    events_before = tuple(persistence.events())

    assert cancel_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="cancelled",
    )
    assert tuple(persistence.events()) == events_before


def test_completed_demand_response_cannot_be_cancelled():
    persistence, entities, engine, backend = _runtime()
    event, _, _, _ = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="completed",
    )
    event.state = "completed"
    _save(persistence, event)

    assert cancel_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="completed",
    ) is False


def test_opt_out_is_idempotent_after_terminal_participation():
    persistence, entities, engine, backend = _runtime()
    event, participation, _, _ = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="opt-out",
    )
    assert opt_out_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="opt-out",
    )
    events_before = tuple(persistence.events())

    assert opt_out_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="opt-out",
    )
    assert tuple(persistence.events()) == events_before
    persisted = persistence.entity("utility_dr_participation", participation.id)
    assert persisted is not None and persisted.state == "opted_out"


def test_active_demand_response_defers_interrupted_service_point():
    persistence, entities, engine, backend = _runtime()
    event, participation, _, _ = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="interrupted",
    )
    event.state = "active"
    point = persistence.entity("utility_service_point", entities.service_point_id)
    assert point is not None
    point.state = "interrupted"
    _save(persistence, event)
    _save(persistence, point)

    assert reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="interrupted",
    ) is False
    persisted = persistence.entity("utility_dr_participation", participation.id)
    assert persisted is not None and persisted.state == "eligible"


def test_completed_demand_response_marks_never_started_participation_missed():
    persistence, entities, engine, backend = _runtime()
    event, participation, _, _ = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="missed",
    )
    event.state = "completed"
    _save(persistence, event)

    assert reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="missed",
    )
    persisted = persistence.entity("utility_dr_participation", participation.id)
    assert persisted is not None and persisted.state == "missed"

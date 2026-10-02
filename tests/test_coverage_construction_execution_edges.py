from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    complete_activity,
    complete_predecessor,
    ensure_inspection,
    finish_execution,
    reconcile_dependency,
    reconcile_inspection,
    record_measurement,
    request_execution_resources,
)
from sose.examples.construction.runtime import (
    ORIGIN,
    activity,
    build_runtime,
    measurement_id,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(*, predecessor_completed=True):
    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        predecessor_completed=predecessor_completed,
    )
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def test_dependency_blocks_then_releases_when_predecessor_completes():
    persistence, entities, engine, _ = _runtime(predecessor_completed=False)

    assert reconcile_dependency(
        persistence,
        engine,
        entities=entities,
    ) is False
    current = activity(persistence, entities.activity_id)
    assert current.state == "blocked_dependency"

    complete_predecessor(
        persistence,
        engine,
        entities=entities,
    )
    assert activity(persistence, entities.predecessor_id).state == "completed"

    assert reconcile_dependency(
        persistence,
        engine,
        entities=entities,
    )
    assert activity(persistence, entities.activity_id).state == "ready"


def test_complete_predecessor_is_idempotent():
    persistence, entities, engine, _ = _runtime(predecessor_completed=True)
    events_before = tuple(persistence.events())

    complete_predecessor(
        persistence,
        engine,
        entities=entities,
    )

    assert tuple(persistence.events()) == events_before


def test_complete_predecessor_rejects_wrong_state():
    persistence, entities, engine, _ = _runtime(predecessor_completed=False)
    predecessor = activity(persistence, entities.predecessor_id)
    predecessor.state = "planned"
    _save(persistence, predecessor)

    with pytest.raises(RuntimeError, match="not at completion boundary"):
        complete_predecessor(
            persistence,
            engine,
            entities=entities,
        )


@pytest.mark.parametrize(
    ("measurement_state", "value"),
    [
        ("captured", 1.0),
        ("recorded", 0.0),
    ],
)
def test_complete_predecessor_requires_valid_measurement_evidence(
    measurement_state, value
):
    persistence, entities, engine, _ = _runtime(predecessor_completed=False)
    measurement = persistence.entity(
        "construction_measurement",
        measurement_id(entities.predecessor_id),
    )
    assert measurement is not None
    measurement.state = measurement_state
    measurement.attributes["value"] = value
    _save(persistence, measurement)

    with pytest.raises(RuntimeError, match="requires durable measurement evidence"):
        complete_predecessor(
            persistence,
            engine,
            entities=entities,
        )


def _ready(persistence, entities, engine):
    assert reconcile_dependency(
        persistence,
        engine,
        entities=entities,
    )
    return activity(persistence, entities.activity_id)


def test_execution_resources_require_material_staging():
    persistence, entities, engine, backend = _runtime()
    current = _ready(persistence, entities, engine)
    assert current.state == "ready"

    with pytest.raises(RuntimeError, match="before material staging"):
        request_execution_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_execution_resource_reconcile_returns_false_from_unhandled_state():
    persistence, entities, engine, backend = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "inspection"
    _save(persistence, current)

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_executing_resource_replay_is_idempotent():
    persistence, entities, engine, backend = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "executing"
    _save(persistence, current)
    events_before = tuple(persistence.events())

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert tuple(persistence.events()) == events_before


def test_site_outage_withdraws_both_resource_ownerships(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    current = _ready(persistence, entities, engine)
    current.attributes["material_staged"] = True
    _save(persistence, current)

    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "construction.site.available"
        else default,
    )

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    current = activity(persistence, entities.activity_id)
    assert current.state == "waiting_resource"
    assert engine.resources.has_request(f"crew:{current.id}:initial") is False
    assert engine.resources.has_request(f"equipment:{current.id}:initial") is False


def test_partial_resource_grant_releases_crew_but_keeps_equipment_waiting():
    persistence, entities, engine, backend = _runtime()
    current = _ready(persistence, entities, engine)
    current.attributes["material_staged"] = True
    _save(persistence, current)

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="equipment",
        request_id="equipment:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    current = activity(persistence, entities.activity_id)
    crew_id = f"crew:{current.id}:initial"
    equipment_id = f"equipment:{current.id}:initial"
    assert engine.resources.has_request(crew_id) is False
    assert engine.resources.has_request(equipment_id)
    assert engine.resources.reservation_for(equipment_id) is None


def test_finish_execution_rejects_wrong_boundary():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="not at finish boundary"):
        finish_execution(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_ensure_inspection_requires_inspection_state():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="not waiting for inspection"):
        ensure_inspection(
            persistence,
            engine,
            entities=entities,
            ordinal=1,
        )


def test_ensure_inspection_is_idempotent():
    persistence, entities, engine, _ = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "inspection"
    _save(persistence, current)

    first = ensure_inspection(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
    )
    count_before = len(persistence.entities())
    second = ensure_inspection(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
    )

    assert second.id == first.id
    assert len(persistence.entities()) == count_before


def test_inspection_rejects_unknown_outcome():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match="unsupported inspection outcome"):
        reconcile_inspection(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            outcome="unknown",
        )


def test_inspection_outside_boundary_without_terminal_occurrence_returns_false():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="pass",
    ) is False


def test_inspection_waits_with_durable_inspector_demand():
    persistence, entities, engine, backend = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "inspection"
    _save(persistence, current)
    occurrence = ensure_inspection(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
    )

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="inspector",
        request_id="inspector:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="pass",
    ) is False
    request_id = f"inspector:{occurrence.id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_measurement_requires_positive_value(value):
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(ValueError, match="measurement must be positive"):
        record_measurement(
            persistence,
            engine,
            entities=entities,
            value=value,
        )


def test_measurement_requires_measured_activity():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match=r"requires Activity\(measured\)"):
        record_measurement(
            persistence,
            engine,
            entities=entities,
            value=1.0,
        )


def test_measurement_creation_is_idempotent():
    persistence, entities, engine, _ = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "measured"
    _save(persistence, current)

    first = record_measurement(
        persistence,
        engine,
        entities=entities,
        value=3.0,
    )
    count_before = len(persistence.entities())
    second = record_measurement(
        persistence,
        engine,
        entities=entities,
        value=99.0,
    )

    assert second.id == first.id
    assert second.attributes["value"] == 3.0
    assert len(persistence.entities()) == count_before


def test_complete_activity_is_idempotent():
    persistence, entities, engine, _ = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "completed"
    _save(persistence, current)
    events_before = tuple(persistence.events())

    complete_activity(
        persistence,
        engine,
        entities=entities,
    )

    assert tuple(persistence.events()) == events_before


def test_complete_activity_rejects_wrong_boundary():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="not at completion boundary"):
        complete_activity(
            persistence,
            engine,
            entities=entities,
        )


def test_complete_activity_requires_measurement_evidence():
    persistence, entities, engine, _ = _runtime()
    current = activity(persistence, entities.activity_id)
    current.state = "measured"
    _save(persistence, current)

    with pytest.raises(RuntimeError, match="requires durable measurement evidence"):
        complete_activity(
            persistence,
            engine,
            entities=entities,
        )

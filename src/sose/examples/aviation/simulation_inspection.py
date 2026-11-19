"""Aviation post-landing inspection reconciliation (split from simulation.py)."""

from __future__ import annotations

from .entities import (
    Inspection,
)
from .simulation_maintenance import (
    ensure_maintenance,
)
from .simulation_runtime import (
    _aircraft,
    _dispatch,
    _entity,
    _flight,
    flow_correlation_id,
    inspection_id,
)


def ensure_inspection(persistence, engine, *, flight_id):
    existing = persistence.entity("aviation_inspection", inspection_id(flight_id))
    if existing is not None:
        return existing
    flight = _flight(persistence, flight_id)
    if flight.state != "inspection":
        raise RuntimeError("inspection requires Flight(inspection)")
    value = engine.context.entities.create(
        Inspection,
        key=("aviation-reference", flight.id, "inspection"),
        state="pending",
        attributes={"flight_id": flight.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def reconcile_inspection(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
    fail=False,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    inspection = ensure_inspection(
        persistence,
        engine,
        flight_id=flight.id,
    )
    request_id = f"inspection-team:{inspection.id}"
    correlation_id = flow_correlation_id(aircraft.id)

    inspection = _progress_inspection_until_terminal(
        persistence,
        engine,
        backend,
        inspection=inspection,
        request_id=request_id,
        fail=fail,
        correlation_id=correlation_id,
    )

    if inspection.state not in {"passed", "failed"}:
        return False

    engine.resources.withdraw(backend, request_id)

    aircraft = _aircraft(persistence, entities)
    flight = _flight(persistence, flight.id)
    if inspection.state == "passed":
        _release_after_passed_inspection(
            persistence,
            engine,
            entities=entities,
            flight_id=flight.id,
            correlation_id=correlation_id,
        )
        return True

    if aircraft.state == "inspection":
        _dispatch(
            engine,
            aircraft,
            "mark_aog",
            key=("aviation-aircraft", aircraft.id, flight.id, "aog"),
            correlation_id=correlation_id,
        )
    ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight.id,
    )
    return False


def _progress_inspection_until_terminal(
    persistence,
    engine,
    backend,
    *,
    inspection,
    request_id: str,
    fail: bool,
    correlation_id: str,
):
    if inspection.state in {"passed", "failed"}:
        return inspection
    if engine.resources.ensure_requested(
        backend,
        resource_name="inspection_team",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return inspection
    inspection = _entity(
        persistence,
        "aviation_inspection",
        inspection.id,
    )
    if inspection.state == "pending":
        _dispatch(
            engine,
            inspection,
            "begin",
            key=("aviation-inspection", inspection.id, "begin"),
            correlation_id=correlation_id,
        )
        inspection = _entity(
            persistence,
            "aviation_inspection",
            inspection.id,
        )
    if inspection.state == "inspecting":
        _dispatch(
            engine,
            inspection,
            "fail_inspection" if fail else "pass_inspection",
            key=(
                "aviation-inspection",
                inspection.id,
                "fail" if fail else "pass",
            ),
            correlation_id=correlation_id,
        )
        inspection = _entity(
            persistence,
            "aviation_inspection",
            inspection.id,
        )
    return inspection


def _release_after_passed_inspection(
    persistence,
    engine,
    *,
    entities,
    flight_id: str,
    correlation_id: str,
) -> None:
    aircraft = _aircraft(persistence, entities)
    if aircraft.state == "inspection":
        _dispatch(
            engine,
            aircraft,
            "release",
            key=("aviation-aircraft", aircraft.id, flight_id, "release"),
            correlation_id=correlation_id,
        )
    flight = _flight(persistence, flight_id)
    if flight.state == "inspection":
        _dispatch(
            engine,
            flight,
            "release",
            key=("aviation", flight.id, "release"),
            correlation_id=correlation_id,
        )

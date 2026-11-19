"""Aviation departure scheduling, dispatch and landing (split from simulation.py)."""

from __future__ import annotations

from .simulation_inspection import (
    ensure_inspection,
)
from .simulation_runtime import (
    LEG1_DEPARTURE_DELAY,
    LEG2_DEPARTURE_DELAY,
    _aircraft,
    _crew,
    _dispatch,
    _flight,
    flow_correlation_id,
)


def schedule_departure(
    persistence,
    engine,
    backend,
    *,
    flight_id,
    delay,
):
    flight = _flight(persistence, flight_id)
    if flight.state != "scheduled":
        return backend.now
    existing = engine.scheduler.find_pending(
        entity_type="aviation_flight",
        entity_id=flight.id,
        name="make_due",
    )
    if existing is not None:
        return existing.work.due_at
    due_at = backend.now + delay
    command = engine.context.commands.create(
        "make_due",
        target=flight,
        due_at=due_at,
        correlation_id=flow_correlation_id(str(flight.attributes["aircraft_id"])),
        key=("aviation", flight.id, "departure-due"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def schedule_rotation(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    first = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=LEG1_DEPARTURE_DELAY,
    )
    second = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg2_id,
        delay=LEG2_DEPARTURE_DELAY,
    )
    return first, second


def _crew_for_flight(entities, flight_id):
    if flight_id == entities.leg1_id:
        return entities.leg1_crew_id
    if flight_id == entities.leg2_id:
        return entities.leg2_crew_id
    raise KeyError(f"unknown reference flight: {flight_id}")


def reconcile_departure(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    crew = _crew(persistence, _crew_for_flight(entities, flight_id))
    correlation_id = flow_correlation_id(aircraft.id)
    request_id = f"flight-crew:{flight.id}"

    if flight.state == "airborne":
        return _reconcile_departure_airborne(
            persistence,
            engine,
            entities=entities,
            flight_id=flight.id,
            aircraft=aircraft,
            correlation_id=correlation_id,
        )
    if flight.state not in {"due", "delayed", "ready"}:
        return False

    if not _departure_prerequisites_ready(
        persistence,
        engine,
        aircraft=aircraft,
        flight=flight,
    ):
        _handle_departure_unavailable(
            engine,
            backend,
            flight=flight,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return False

    if flight.state == "delayed":
        _dispatch(
            engine,
            flight,
            "resume",
            key=("aviation", flight.id, "resume"),
            correlation_id=correlation_id,
        )
        flight = _flight(persistence, flight.id)

    if not _ensure_crew_reservation(
        persistence,
        engine,
        backend,
        crew=crew,
        request_id=request_id,
        correlation_id=correlation_id,
    ):
        return False

    _progress_departure_dispatch(
        persistence,
        engine,
        entities=entities,
        flight_id=flight.id,
        correlation_id=correlation_id,
    )
    return _flight(persistence, flight.id).state == "airborne"


def land_flight(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    crew = _crew(persistence, _crew_for_flight(entities, flight_id))
    if flight.state not in {"airborne", "landed", "inspection"}:
        raise RuntimeError(f"flight cannot reconcile landing from {flight.state}")
    if aircraft.state not in {"airborne", "inspection"}:
        raise RuntimeError(f"aircraft cannot reconcile landing from {aircraft.state}")
    correlation_id = flow_correlation_id(aircraft.id)

    if flight.state == "airborne":
        _dispatch(
            engine,
            flight,
            "land",
            key=("aviation", flight.id, "land"),
            correlation_id=correlation_id,
        )
    aircraft = _aircraft(persistence, entities)
    if aircraft.state == "airborne":
        _dispatch(
            engine,
            aircraft,
            "land",
            key=("aviation-aircraft", aircraft.id, flight.id, "land"),
            correlation_id=correlation_id,
        )

    crew = _crew(persistence, crew.id)
    if crew.state == "active":
        _dispatch(
            engine,
            crew,
            "release",
            key=("aviation-crew", crew.id, "release"),
            correlation_id=correlation_id,
        )
    engine.resources.withdraw(backend, f"flight-crew:{flight.id}")

    flight = _flight(persistence, flight.id)
    if flight.state == "landed":
        _dispatch(
            engine,
            flight,
            "inspect",
            key=("aviation", flight.id, "inspect"),
            correlation_id=correlation_id,
        )
    return ensure_inspection(
        persistence,
        engine,
        flight_id=flight.id,
    )


def _reconcile_departure_airborne(
    persistence,
    engine,
    *,
    entities,
    flight_id: str,
    aircraft,
    correlation_id: str,
) -> bool:
    if aircraft.state == "assigned":
        _dispatch(
            engine,
            aircraft,
            "dispatch",
            key=("aviation-aircraft", aircraft.id, flight_id, "dispatch"),
            correlation_id=correlation_id,
        )
        aircraft = _aircraft(persistence, entities)
    return aircraft.state == "airborne"


def _departure_prerequisites_ready(
    persistence,
    engine,
    *,
    aircraft,
    flight,
) -> bool:
    predecessor_id = flight.attributes.get("predecessor_flight_id")
    predecessor_ready = True
    if predecessor_id is not None:
        predecessor = _flight(persistence, str(predecessor_id))
        predecessor_ready = predecessor.state == "released"
    departure_available = engine.context.scenarios.attribute(
        "aviation.departure.available",
        True,
    )
    crew_available = engine.context.scenarios.attribute(
        "aviation.crew.available",
        True,
    )
    aircraft_ready = aircraft.state in {"available", "released"}
    return predecessor_ready and departure_available and crew_available and aircraft_ready


def _handle_departure_unavailable(
    engine,
    backend,
    *,
    flight,
    request_id: str,
    correlation_id: str,
) -> None:
    engine.resources.withdraw(backend, request_id)
    if flight.state not in {"due", "ready"}:
        return
    _dispatch(
        engine,
        flight,
        "delay",
        key=("aviation", flight.id, "delay"),
        correlation_id=correlation_id,
    )


def _ensure_crew_reservation(
    persistence,
    engine,
    backend,
    *,
    crew,
    request_id: str,
    correlation_id: str,
) -> bool:
    if crew.state == "planned":
        _dispatch(
            engine,
            crew,
            "reserve",
            key=("aviation-crew", crew.id, "reserve"),
            correlation_id=correlation_id,
        )
        crew = _crew(persistence, crew.id)
    if engine.resources.ensure_requested(
        backend,
        resource_name="flight_crew",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False
    crew = _crew(persistence, crew.id)
    if crew.state == "reserved":
        _dispatch(
            engine,
            crew,
            "activate",
            key=("aviation-crew", crew.id, "activate"),
            correlation_id=correlation_id,
        )
    return True


def _progress_departure_dispatch(
    persistence,
    engine,
    *,
    entities,
    flight_id: str,
    correlation_id: str,
) -> None:
    flight = _flight(persistence, flight_id)
    if flight.state == "due":
        _dispatch(
            engine,
            flight,
            "mark_ready",
            key=("aviation", flight.id, "ready"),
            correlation_id=correlation_id,
        )
    aircraft = _aircraft(persistence, entities)
    if aircraft.state in {"available", "released"}:
        _dispatch(
            engine,
            aircraft,
            "assign",
            key=("aviation-aircraft", aircraft.id, flight.id, "assign"),
            correlation_id=correlation_id,
        )
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    if flight.state == "ready" and aircraft.state == "assigned":
        _dispatch(
            engine,
            flight,
            "depart",
            key=("aviation", flight.id, "depart"),
            correlation_id=correlation_id,
        )
        _dispatch(
            engine,
            aircraft,
            "dispatch",
            key=("aviation-aircraft", aircraft.id, flight.id, "dispatch"),
            correlation_id=correlation_id,
        )

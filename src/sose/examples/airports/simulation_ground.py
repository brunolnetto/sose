"""Airport ground-service and baggage reconciliation (split from simulation.py)."""

from __future__ import annotations

from .simulation_runtime import (
    _baggage,
    _dispatch,
    _service_task,
    _turnaround,
    flow_correlation_id,
)


def reconcile_ground_service(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    turnaround = _turnaround(persistence, entities)
    task = _service_task(persistence, entities)
    request_id = f"ground-team:{task.id}"

    if task.state == "completed":
        engine.resources.withdraw(backend, request_id)
        turnaround = _turnaround(persistence, entities)
        if turnaround.state == "servicing":
            _dispatch(
                engine,
                turnaround,
                "service_ready",
                key=("airport", turnaround.id, task.id, "service-ready"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        return True

    if turnaround.state == "gate_assigned":
        _dispatch(
            engine,
            turnaround,
            "start_deboarding",
            key=("airport", turnaround.id, "deboarding"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
        turnaround = _turnaround(persistence, entities)
    if turnaround.state == "deboarding":
        _dispatch(
            engine,
            turnaround,
            "start_servicing",
            key=("airport", turnaround.id, "start-servicing"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
    turnaround = _turnaround(persistence, entities)
    if turnaround.state != "servicing":
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="ground_team",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    task = _service_task(persistence, entities)
    correlation_id = flow_correlation_id(turnaround.id)
    if task.state == "pending":
        _dispatch(
            engine,
            task,
            "start",
            key=("airport-service", task.id, "start"),
            correlation_id=correlation_id,
        )
        task = _service_task(persistence, entities)
    if task.state == "in_progress":
        _dispatch(
            engine,
            task,
            "complete",
            key=("airport-service", task.id, "complete"),
            correlation_id=correlation_id,
        )
    return reconcile_ground_service(
        persistence,
        engine,
        backend,
        entities=entities,
    )


def reconcile_baggage(
    persistence,
    engine,
    *,
    entities,
    delayed=False,
):
    turnaround = _turnaround(persistence, entities)
    baggage = _baggage(persistence, entities)
    if turnaround.state not in {"boarding", "waiting_baggage"}:
        return False
    correlation_id = flow_correlation_id(turnaround.id)

    # The baggage delay may commit before the turnaround wait-state. Preserve
    # that operational fact before allowing recovery to mark baggage ready.
    if baggage.state == "delayed" and turnaround.state == "boarding":
        _dispatch(
            engine,
            turnaround,
            "baggage_delayed",
            key=("airport", turnaround.id, baggage.id, "baggage-delayed"),
            correlation_id=correlation_id,
        )
        turnaround = _turnaround(persistence, entities)

    if baggage.state == "pending":
        _dispatch(
            engine,
            baggage,
            "start",
            key=("airport-baggage", baggage.id, "start"),
            correlation_id=correlation_id,
        )
        baggage = _baggage(persistence, entities)

    if delayed and baggage.state == "transferring":
        _dispatch(
            engine,
            baggage,
            "delay",
            key=("airport-baggage", baggage.id, "delay"),
            correlation_id=correlation_id,
        )
        if turnaround.state == "boarding":
            _dispatch(
                engine,
                turnaround,
                "baggage_delayed",
                key=("airport", turnaround.id, baggage.id, "baggage-delayed"),
                correlation_id=correlation_id,
            )
        return False

    if baggage.state in {"transferring", "delayed"}:
        _dispatch(
            engine,
            baggage,
            "mark_ready",
            key=("airport-baggage", baggage.id, "mark-ready"),
            correlation_id=correlation_id,
        )
    turnaround = _turnaround(persistence, entities)
    if turnaround.state == "waiting_baggage":
        _dispatch(
            engine,
            turnaround,
            "baggage_ready",
            key=("airport", turnaround.id, baggage.id, "baggage-ready"),
            correlation_id=correlation_id,
        )
    return True

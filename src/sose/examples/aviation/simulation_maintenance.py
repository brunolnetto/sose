"""Aviation AOG maintenance work orders and preemption (split from simulation.py)."""

from __future__ import annotations

from .entities import (
    MaintenanceWorkOrder,
    PartDemand,
)
from .simulation_runtime import (
    _aircraft,
    _dispatch,
    _entity,
    _flight,
    flow_correlation_id,
    maintenance_work_order_id,
    part_demand_id,
)


def ensure_maintenance(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    work_id = maintenance_work_order_id(flight_id)
    work = persistence.entity("aviation_maintenance_work_order", work_id)
    demand = persistence.entity("aviation_part_demand", part_demand_id(flight_id))
    aircraft = _aircraft(persistence, entities)
    correlation_id = flow_correlation_id(aircraft.id)

    if work is None:
        work = engine.context.entities.create(
            MaintenanceWorkOrder,
            key=("aviation-reference", flight_id, "maintenance"),
            state="planned",
            attributes={
                "flight_id": flight_id,
                "aircraft_id": aircraft.id,
                "priority": 1,
                "aog": True,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(work)
    if demand is None:
        demand = engine.context.entities.create(
            PartDemand,
            key=("aviation-reference", flight_id, "part-demand"),
            state="open",
            attributes={
                "flight_id": flight_id,
                "sku": "AOG-PART",
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(demand)

    work = _entity(persistence, "aviation_maintenance_work_order", work.id)
    if work.state == "planned":
        _dispatch(
            engine,
            work,
            "release",
            key=("aviation-maintenance", work.id, "release"),
            correlation_id=correlation_id,
        )
        work = _entity(persistence, "aviation_maintenance_work_order", work.id)

    queue_item_id = f"maintenance:{work.id}"
    if not any(i.item_id == queue_item_id for i in persistence.store_items()) and not any(
        i.item_id == queue_item_id for i in persistence.store_put_intents()
    ) and not any(
        r.item.item_id == queue_item_id for r in persistence.store_get_results()
    ):
        engine.stores.put(
            backend,
            store_name="maintenance_queue",
            item_id=queue_item_id,
            value={"work_order_id": work.id},
            priority=1,
            requested_at=backend.now,
        )
        backend.run_until(backend.now)

    return work, demand


def reconcile_aog_maintenance(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight_id,
    )
    if demand.state != "issued":
        return False
    correlation_id = flow_correlation_id(aircraft.id)

    if work.state == "released":
        _dispatch(
            engine,
            work,
            "wait_bay",
            key=("aviation-maintenance", work.id, "wait-bay"),
            correlation_id=correlation_id,
        )
        work = _entity(persistence, "aviation_maintenance_work_order", work.id)

    if not _ensure_maintenance_queue_selection(
        persistence,
        engine,
        backend,
        work_id=work.id,
    ):
        return False

    request_id = f"maintenance-bay:{work.id}"
    if engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="maintenance_bay",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    ) is None:
        return False

    _start_aog_maintenance_if_ready(
        persistence,
        engine,
        entities=entities,
        work_id=work.id,
        flight_id=flight.id,
        correlation_id=correlation_id,
    )
    return True


def _ensure_maintenance_queue_selection(
    persistence,
    engine,
    backend,
    *,
    work_id: str,
) -> bool:
    queue_request = f"maintenance-pick:{work_id}"
    queue_result = engine.stores.selection(queue_request)
    if queue_result is not None:
        return True
    queue_items = sorted(
        (
            item
            for item in persistence.store_items()
            if item.store_name == "maintenance_queue"
        ),
        key=lambda item: (item.priority, item.sequence, item.item_id),
    )
    if not queue_items or str(queue_items[0].value["work_order_id"]) != work_id:
        return False
    return (
        engine.stores.ensure_selection(
            backend,
            store_name="maintenance_queue",
            request_id=queue_request,
            requested_at=backend.now,
        )
        is not None
    )


def _start_aog_maintenance_if_ready(
    persistence,
    engine,
    *,
    entities,
    work_id: str,
    flight_id: str,
    correlation_id: str,
) -> None:
    work = _entity(persistence, "aviation_maintenance_work_order", work_id)
    if work.state == "waiting_bay":
        _dispatch(
            engine,
            work,
            "start",
            key=("aviation-maintenance", work.id, "start"),
            correlation_id=correlation_id,
        )
    aircraft = _aircraft(persistence, entities)
    if aircraft.state != "aog":
        return
    _dispatch(
        engine,
        aircraft,
        "start_maintenance",
        key=("aviation-aircraft", aircraft.id, flight_id, "maintenance"),
        correlation_id=correlation_id,
    )


def complete_aog_maintenance(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    work = _entity(
        persistence,
        "aviation_maintenance_work_order",
        maintenance_work_order_id(flight_id),
    )
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    correlation_id = flow_correlation_id(aircraft.id)

    if work.state == "in_progress":
        _dispatch(
            engine,
            work,
            "complete",
            key=("aviation-maintenance", work.id, "complete"),
            correlation_id=correlation_id,
        )
        work = _entity(
            persistence,
            "aviation_maintenance_work_order",
            work.id,
        )
    if work.state != "completed":
        raise RuntimeError("AOG maintenance is not complete or active")

    aircraft = _aircraft(persistence, entities)
    if aircraft.state == "maintenance":
        _dispatch(
            engine,
            aircraft,
            "finish_maintenance",
            key=("aviation-aircraft", aircraft.id, flight.id, "maintenance-complete"),
            correlation_id=correlation_id,
        )
        aircraft = _aircraft(persistence, entities)
    if aircraft.state != "released":
        raise RuntimeError("completed maintenance did not release aircraft")

    flight = _flight(persistence, flight_id)
    if flight.state == "inspection":
        _dispatch(
            engine,
            flight,
            "release",
            key=("aviation", flight.id, "maintenance-release"),
            correlation_id=correlation_id,
        )
    engine.preemptive_resources.withdraw(
        backend,
        f"maintenance-bay:{work.id}",
    )
    return _flight(persistence, flight_id).state == "released"


def preemption_result_for(persistence, *, preempting_request_id):
    return next(
        (
            result
            for result in persistence.resource_preemption_results()
            if result.preempting_request_id == preempting_request_id
        ),
        None,
    )

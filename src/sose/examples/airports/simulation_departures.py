"""Airport departure slots, queueing and departure (split from simulation.py)."""

from __future__ import annotations

from .simulation_runtime import (
    DEPARTURE_SLOT_DELAY,
    _baggage,
    _dispatch,
    _gate_assignment,
    _service_task,
    _slot,
    _turnaround,
    flow_correlation_id,
)


def schedule_departure_slot(
    persistence,
    engine,
    backend,
    *,
    entities,
    delay=DEPARTURE_SLOT_DELAY,
):
    slot = _slot(persistence, entities)
    if slot.state == "planned":
        _dispatch(
            engine,
            slot,
            "schedule",
            key=("airport-slot", slot.id, "schedule"),
            correlation_id=flow_correlation_id(entities.turnaround_id),
        )
        slot = _slot(persistence, entities)
    existing = engine.scheduler.find_pending(
        entity_type="airport_departure_slot",
        entity_id=slot.id,
        name="make_due",
    )
    if slot.state == "scheduled" and existing is None:
        due_at = backend.now + delay
        command = engine.context.commands.create(
            "make_due",
            target=slot,
            due_at=due_at,
            correlation_id=flow_correlation_id(entities.turnaround_id),
            key=("airport-slot", slot.id, "due"),
        )
        engine.context.schedules.at(due_at, command=command)
        return due_at
    if existing is not None:
        return existing.work.due_at
    return backend.now


def queue_departure(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    turnaround = _turnaround(persistence, entities)
    baggage = _baggage(persistence, entities)
    task = _service_task(persistence, entities)
    _validate_departure_queue_preconditions(
        turnaround=turnaround,
        baggage=baggage,
        task=task,
    )

    if turnaround.state == "boarding":
        _dispatch(
            engine,
            turnaround,
            "start_boarding",
            key=("airport", turnaround.id, "boarding-complete"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
    item_id = f"departure:{turnaround.id}"
    if _departure_item_already_observed(persistence, item_id):
        return
    engine.stores.put(
        backend,
        store_name="departure_queue",
        item_id=item_id,
        value={"turnaround_id": turnaround.id},
        priority=int(turnaround.attributes["departure_priority"]),
        requested_at=backend.now,
    )
    backend.run_until(backend.now)


def _validate_departure_queue_preconditions(*, turnaround, baggage, task) -> None:
    if turnaround.state not in {"boarding", "waiting_slot"}:
        raise RuntimeError(
            "departure queue requires FlightTurnaround(boarding/waiting_slot), "
            f"got {turnaround.state}"
        )
    if baggage.state != "ready":
        raise RuntimeError("departure queue requires BaggageFlow(ready)")
    if task.state != "completed":
        raise RuntimeError("departure queue requires GroundServiceTask(completed)")


def _departure_item_already_observed(persistence, item_id: str) -> bool:
    queued = any(item.item_id == item_id for item in persistence.store_items())
    consumed = any(
        result.item.item_id == item_id
        for result in persistence.store_get_results()
    )
    pending = any(intent.item_id == item_id for intent in persistence.store_put_intents())
    return queued or consumed or pending


def reconcile_departure(
    persistence,
    engine,
    backend,
    *,
    entities,
    dispatcher_id="departure",
):
    turnaround = _turnaround(persistence, entities)
    slot = _slot(persistence, entities)
    gate = _gate_assignment(persistence, entities)
    request_id = f"tug:{turnaround.id}"

    if _reconcile_departed_turnaround(
        engine,
        backend,
        turnaround=turnaround,
        gate=gate,
        request_id=request_id,
    ):
        return True

    # slot_ready / slot consumption may commit before the final departure
    # transition. Recovery must finish pushback without requiring a fresh slot
    # or a second departure-queue item.
    if turnaround.state == "pushback":
        _dispatch_depart_and_recurse(
            persistence,
            engine,
            backend,
            entities=entities,
            turnaround=turnaround,
            dispatcher_id=dispatcher_id,
        )
        return reconcile_departure(
            persistence,
            engine,
            backend,
            entities=entities,
            dispatcher_id=dispatcher_id,
        )

    if turnaround.state != "waiting_slot":
        return False
    if slot.state not in {"due", "delayed"}:
        return False

    if not _ensure_departure_capacity(
        persistence,
        engine,
        backend,
        entities=entities,
        turnaround=turnaround,
        slot=slot,
        request_id=request_id,
    ):
        return False

    result = _ensure_departure_queue_selection(
        persistence,
        engine,
        backend,
        turnaround=turnaround,
        request_id=request_id,
        dispatcher_id=dispatcher_id,
    )
    if result is None:
        return False

    selected = str(result.item.value["turnaround_id"])
    if selected != turnaround.id:
        engine.resources.withdraw(backend, request_id)
        return False

    _progress_departure_entities(
        persistence,
        engine,
        entities=entities,
        turnaround=turnaround,
        slot=slot,
    )
    return reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        dispatcher_id=dispatcher_id,
    )


def _reconcile_departed_turnaround(
    engine,
    backend,
    *,
    turnaround,
    gate,
    request_id: str,
) -> bool:
    if turnaround.state != "departed":
        return False
    engine.resources.withdraw(backend, request_id)
    engine.resources.withdraw(backend, f"gate:{turnaround.id}")
    if gate.state == "occupied":
        _dispatch(
            engine,
            gate,
            "release",
            key=("airport-gate", gate.id, "release"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
    return True


def _dispatch_depart_and_recurse(
    persistence,
    engine,
    backend,
    *,
    entities,
    turnaround,
    dispatcher_id: str,
) -> None:
    _dispatch(
        engine,
        turnaround,
        "depart",
        key=("airport", turnaround.id, "depart"),
        correlation_id=flow_correlation_id(turnaround.id),
    )


def _ensure_departure_capacity(
    persistence,
    engine,
    backend,
    *,
    entities,
    turnaround,
    slot,
    request_id: str,
) -> bool:
    if not engine.context.scenarios.attribute("airport.departure.available", True):
        if slot.state == "due":
            _dispatch(
                engine,
                slot,
                "delay",
                key=("airport-slot", slot.id, "weather-delay"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        engine.resources.withdraw(backend, request_id)
        return False
    return (
        engine.resources.ensure_requested(
            backend,
            resource_name="tug",
            request_id=request_id,
            requested_at=backend.now,
            priority=int(turnaround.attributes["departure_priority"]),
        )
        is not None
    )


def _ensure_departure_queue_selection(
    persistence,
    engine,
    backend,
    *,
    turnaround,
    request_id: str,
    dispatcher_id: str,
):
    get_id = f"departure-pick:{dispatcher_id}"
    result = engine.stores.selection(get_id)

    # Priority ownership remains domain-visible: only the current durable head
    # may initiate selection. Once StoreGetResult commits, recovery continues
    # from that result even though the queue item is gone.
    if result is not None:
        return result
    queued_items = sorted(
        (
            item
            for item in persistence.store_items()
            if item.store_name == "departure_queue"
        ),
        key=lambda item: (item.priority, item.sequence, item.item_id),
    )
    if not queued_items:
        engine.resources.withdraw(backend, request_id)
        return None
    if str(queued_items[0].value["turnaround_id"]) != turnaround.id:
        engine.resources.withdraw(backend, request_id)
        return None
    result = engine.stores.ensure_selection(
        backend,
        store_name="departure_queue",
        request_id=get_id,
        requested_at=backend.now,
    )
    if result is None:
        engine.resources.withdraw(backend, request_id)
    return result


def _progress_departure_entities(
    persistence,
    engine,
    *,
    entities,
    turnaround,
    slot,
) -> None:
    correlation_id = flow_correlation_id(turnaround.id)
    refreshed_turnaround = _turnaround(persistence, entities)
    if refreshed_turnaround.state == "waiting_slot":
        _dispatch(
            engine,
            refreshed_turnaround,
            "slot_ready",
            key=("airport", refreshed_turnaround.id, slot.id, "slot-ready"),
            correlation_id=correlation_id,
        )
    refreshed_slot = _slot(persistence, entities)
    if refreshed_slot.state in {"due", "delayed"}:
        _dispatch(
            engine,
            refreshed_slot,
            "consume",
            key=("airport-slot", refreshed_slot.id, "consume"),
            correlation_id=correlation_id,
        )
    refreshed_turnaround = _turnaround(persistence, entities)
    if refreshed_turnaround.state == "pushback":
        _dispatch(
            engine,
            refreshed_turnaround,
            "depart",
            key=("airport", refreshed_turnaround.id, "depart"),
            correlation_id=correlation_id,
        )

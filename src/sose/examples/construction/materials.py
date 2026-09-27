from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .runtime import (
    ConstructionEntities,
    activity,
    dispatch,
    flow_correlation_id,
    save_activity_attributes,
)


def seed_material(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    if not any(
        item.item_id == "construction-material-lot-1"
        for item in persistence.store_items()
    ) and not any(
        intent.item_id == "construction-material-lot-1"
        for intent in persistence.store_put_intents()
    ):
        engine.stores.put(
            backend,
            store_name="material_lots",
            item_id="construction-material-lot-1",
            value={"sku": "structural-steel", "quantity": quantity},
            requested_at=backend.now,
        )

    request_id = "seed-construction-material"
    if not any(
        intent.request_id == request_id
        for intent in persistence.container_operation_intents()
    ) and not any(
        result.request_id == request_id
        for result in persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="material_quantity",
            request_id=request_id,
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)


def material_feasible(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
) -> bool:
    current = activity(persistence, entities.activity_id)
    if bool(current.attributes.get("material_staged", False)):
        return True
    if not engine.context.scenarios.attribute(
        "construction.material.available",
        True,
    ):
        return False

    quantity = float(current.attributes["quantity"])
    lots = [
        item
        for item in persistence.store_items()
        if item.store_name == "material_lots"
    ]
    next_lot = lots[0] if lots else None
    has_usable_lot = (
        next_lot is not None
        and float(next_lot.value.get("quantity", 0.0)) >= quantity
    )
    level = next(
        (
            state.level
            for state in persistence.container_states()
            if state.name == "material_quantity"
        ),
        0.0,
    )
    return has_usable_lot and level >= quantity


def reconcile_material_availability(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
) -> bool:
    current = activity(persistence, entities.activity_id)
    feasible = material_feasible(
        persistence,
        engine,
        entities=entities,
    )
    correlation_id = flow_correlation_id(current.id)

    if current.state == "ready" and not feasible:
        dispatch(
            engine,
            current,
            "wait_material",
            key=("construction", current.id, "wait-material"),
            correlation_id=correlation_id,
        )
        return False

    if current.state == "waiting_material" and feasible:
        dispatch(
            engine,
            current,
            "material_ready",
            key=("construction", current.id, "material-ready"),
            correlation_id=correlation_id,
        )
        return True

    return feasible


def stage_material(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ConstructionEntities,
) -> bool:
    current = activity(persistence, entities.activity_id)
    if bool(current.attributes.get("material_staged", False)):
        return True
    if current.state != "ready":
        return False
    if not material_feasible(
        persistence,
        engine,
        entities=entities,
    ):
        reconcile_material_availability(
            persistence,
            engine,
            entities=entities,
        )
        return False

    quantity = float(current.attributes["quantity"])
    lot_request = f"stage-lot:{current.id}"
    qty_request = f"stage-quantity:{current.id}"

    if not any(
        result.request_id == lot_request
        for result in persistence.store_get_results()
    ):
        if not any(
            request.request_id == lot_request
            for request in persistence.store_get_requests()
        ):
            engine.stores.get(
                backend,
                store_name="material_lots",
                request_id=lot_request,
                requested_at=backend.now,
            )

    if not any(
        result.request_id == qty_request
        for result in persistence.container_operation_results()
    ):
        if not any(
            intent.request_id == qty_request
            for intent in persistence.container_operation_intents()
        ):
            engine.containers.get(
                backend,
                container_name="material_quantity",
                request_id=qty_request,
                amount=quantity,
                requested_at=backend.now,
            )

    backend.run_until(backend.now)
    lot_result = next(
        (
            result
            for result in persistence.store_get_results()
            if result.request_id == lot_request
        ),
        None,
    )
    qty_done = any(
        result.request_id == qty_request
        for result in persistence.container_operation_results()
    )
    if lot_result is None or not qty_done:
        return False

    save_activity_attributes(
        persistence,
        current,
        material_staged=True,
        staged_material_lot=lot_result.item.item_id,
        staged_quantity=quantity,
    )
    return True

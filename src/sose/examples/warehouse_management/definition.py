from __future__ import annotations

from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import WarehouseManagementConfig
from .simulation import (
    arrive_truck,
    assign_forklift,
    build_runtime,
    complete_unload,
    seed_reference,
    start_transfer,
)


def _build(
    persistence: Persistence,
    config: WarehouseManagementConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: WarehouseManagementConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        origin_on_hand=config.origin_on_hand,
        transfer_quantity=config.transfer_quantity,
        dock_count=config.dock_count,
        forklift_count=config.forklift_count,
        planned_completion_hours=config.planned_completion_hours,
    )


def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_transfer:
        return
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    if shipment is None:
        raise RuntimeError("configured warehouse shipment was not persisted")
    if shipment.state == "completed":
        return
    if shipment.state == "planned":
        start_transfer(persistence, engine, entities=entities)
        return
    if shipment.state in {"in_transit", "arrived", "delayed"}:
        arrive_truck(persistence, engine, entities=entities)
        return
    if shipment.state == "docked":
        assign_forklift(persistence, engine, entities=entities)
        return
    if shipment.state == "handling":
        complete_unload(persistence, engine, entities=entities)


definition = DomainDefinition(
    name="warehouse_management",
    description=(
        "Multi-site warehouse operations reference domain covering stock, shipments, "
        "truck arrivals, dock occupancy, forklift assignment, and delivery KPIs."
    ),
    config_model=WarehouseManagementConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(
        ["tick_step", "random_seed", "auto_progress_transfer"]
    ),
)

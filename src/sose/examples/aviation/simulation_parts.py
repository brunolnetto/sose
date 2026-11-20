"""Aviation spare-part seeding and part-demand issue (split from simulation.py)."""

from __future__ import annotations

from .simulation_maintenance import (
    ensure_maintenance,
)
from .simulation_runtime import (
    _dispatch,
    _entity,
    flow_correlation_id,
)


def seed_spare_part(
    persistence,
    engine,
    backend,
    *,
    item_id="aog-part-lot-1",
):
    if not any(i.item_id == item_id for i in persistence.store_items()) and not any(
        i.item_id == item_id for i in persistence.store_put_intents()
    ):
        engine.stores.put(
            backend,
            store_name="part_lots",
            item_id=item_id,
            value={"sku": "AOG-PART"},
            priority=1,
            requested_at=backend.now,
        )
        backend.run_until(backend.now)


def reconcile_part_issue(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight_id,
    )
    correlation_id = flow_correlation_id(entities.aircraft_id)

    if demand.state == "issued":
        if work.state == "waiting_part":
            _dispatch(
                engine,
                work,
                "part_ready",
                key=("aviation-maintenance", work.id, "part-ready"),
                correlation_id=correlation_id,
            )
        return True

    if work.state == "released":
        _dispatch(
            engine,
            work,
            "wait_part",
            key=("aviation-maintenance", work.id, "wait-part"),
            correlation_id=correlation_id,
        )

    result = _ensure_part_issue_selection(
        persistence,
        engine,
        backend,
        demand_id=demand.id,
    )
    if result is None:
        return False

    demand = _entity(persistence, "aviation_part_demand", demand.id)
    if demand.state == "open":
        _dispatch(
            engine,
            demand,
            "allocate",
            key=("aviation-part", demand.id, "allocate"),
            correlation_id=correlation_id,
        )
        demand = _entity(persistence, "aviation_part_demand", demand.id)
    if demand.state != "allocated":
        raise RuntimeError(
            f"part demand cannot reconcile issue from {demand.state}"
        )
    _dispatch(
        engine,
        demand,
        "issue",
        key=("aviation-part", demand.id, "issue"),
        correlation_id=correlation_id,
    )
    return reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight_id,
    )


def _ensure_part_issue_selection(
    persistence,
    engine,
    backend,
    *,
    demand_id: str,
):
    result_id = f"part-issue:{demand_id}"
    result = engine.stores.selection(result_id)
    if result is not None:
        return result
    part_items = sorted(
        (i for i in persistence.store_items() if i.store_name == "part_lots"),
        key=lambda item: (item.priority, item.sequence, item.item_id),
    )
    if not part_items:
        return None
    return engine.stores.ensure_selection(
        backend,
        store_name="part_lots",
        request_id=result_id,
        requested_at=backend.now,
    )


def _progress_part_demand_issue(
    persistence,
    engine,
    *,
    demand_id: str,
    correlation_id: str,
) -> None:
    demand = _entity(persistence, "aviation_part_demand", demand_id)
    if demand.state == "open":
        _dispatch(
            engine,
            demand,
            "allocate",
            key=("aviation-part", demand.id, "allocate"),
            correlation_id=correlation_id,
        )
        demand = _entity(persistence, "aviation_part_demand", demand.id)
    if demand.state == "allocated":
        _dispatch(
            engine,
            demand,
            "issue",
            key=("aviation-part", demand.id, "issue"),
            correlation_id=correlation_id,
        )

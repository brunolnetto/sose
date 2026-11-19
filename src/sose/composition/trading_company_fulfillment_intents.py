"""Warehouse fulfillment/inventory intent executors, split from trading_company_customer."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite

from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.warehouse_management import simulation as wm

from .model import BoundaryMessage
from .trading_company_customer_support import _causal_command_scope, _IntentExecution


def _require_request_boundary_source(
    persistence: MemoryPersistence, intent: Command, payload, sales_order_id: str,
) -> None:
    with persistence.transaction() as uow:
        source = uow.get_boundary_message(intent.causation_id)
    if (
        source is None
        or source.contract_key not in {
            "o2c.fulfillment_requested.v1", "o2c.fulfillment_requested.v2",
        }
        or source.source_domain != "order_to_cash"
        or source.destination_domain != "warehouse_fulfillment"
        or source.source_identity != sales_order_id
        or source.payload().get("order_id") != sales_order_id
        or source.message_id != payload.get("boundary_message_id")
        or source.correlation_id != intent.correlation_id
    ):
        raise ValueError("fulfillment request boundary source identity mismatch")


def _require_requested_quantity_and_sku(payload) -> tuple[int | float, str]:
    quantity = payload.get("requested_quantity")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        or not isfinite(float(quantity))
        or float(quantity) <= 0
    ):
        raise ValueError("fulfillment request has invalid requested_quantity")
    sku = payload.get("sku")
    if not isinstance(sku, str) or not sku.strip():
        raise ValueError("fulfillment request has invalid sku")
    return quantity, sku


def _require_replay_matches_order(existing, *, sku: str, quantity: int | float) -> None:
    attributes = existing.attributes
    if (
        attributes.get("inventory_owner") != "warehouse_management"
        or attributes.get("requested_sku") != sku
        or float(attributes.get("requested_quantity", -1)) != float(quantity)
    ):
        raise ValueError("replayed fulfillment request conflicts with durable order")


def _materialize_fulfillment_from_request(
    persistence: MemoryPersistence,
    *,
    intent: Command,
    expected_sales_order_id: str,
    organization_id: str,
) -> fulfillment.WarehouseEntities:
    """Derive composed WF business state from immutable O2C payload, never a fixture."""
    payload = intent.payload
    sales_order_id = payload.get("order_id")
    if not isinstance(sales_order_id, str) or sales_order_id != expected_sales_order_id:
        raise ValueError("fulfillment request source sales order mismatch")
    # Boundary payload and immutable producer provenance are sufficient even
    # when O2C state lives in a separate authoritative store.
    if intent.causation_id is not None:
        _require_request_boundary_source(persistence, intent, payload, sales_order_id)
    request_key = f"order:{sales_order_id}"
    derived_id = deterministic_id(
        "entity", "warehouse_fulfillment_order", "warehouse-reference", request_key
    )
    if payload.get("fulfillment_order_id") != derived_id or intent.entity_id != derived_id:
        raise ValueError("fulfillment request identity mismatch")
    quantity, sku = _require_requested_quantity_and_sku(payload)

    existing = persistence.entity("warehouse_fulfillment_order", derived_id)
    if existing is not None:
        _require_replay_matches_order(existing, sku=sku, quantity=quantity)
        return fulfillment.WarehouseEntities(order_id=derived_id, lot_ids=())

    created = fulfillment.seed_composed_reference(
        persistence,
        owner_id=organization_id,
        now=intent.due_at,
        requested_quantity=float(quantity),
        requested_sku=sku,
        request_key=request_key,
    )
    if created.order_id != derived_id:
        raise RuntimeError("payload-derived fulfillment identity is not deterministic")
    return created


def _execute_request_fulfillment_inventory(run: _IntentExecution) -> None:
    persistence, intent, fixtures = run.persistence, run.intent, run.fixtures
    order_ref = _materialize_fulfillment_from_request(
        persistence, intent=intent,
        expected_sales_order_id=fixtures.o2c.order_id,
        organization_id=run.organization_id,
    )
    if order_ref.order_id != fixtures.fulfillment.order_id:
        raise ValueError("composed fulfillment request differs from current workflow")
    order = persistence.entity("warehouse_fulfillment_order", order_ref.order_id)
    if order is None or order.state != "requested":
        raise RuntimeError("inventory reservation requires requested fulfillment order")


def _execute_reserve_fulfillment_inventory(run: _IntentExecution) -> None:
    persistence, intent = run.persistence, run.intent
    _, engine = wm.build_runtime(persistence, owner_id=run.organization_id, now=intent.due_at)
    applied = wm.reserve_external_stock(
        persistence,
        engine,
        stock_id=str(intent.payload["stock_id"]),
        quantity=float(intent.payload["quantity"]),
        sku=str(intent.payload["sku"]),
        reservation_reference=str(intent.payload["reservation_reference"]),
        caused_by=intent,
        correlation_id=run.correlation_id,
    )
    if not applied:
        stock = persistence.entity(
            "warehouse_management_stock",
            str(intent.payload["stock_id"]),
        )
        reservations = (
            {}
            if stock is None
            else dict(stock.attributes.get("external_reservations", {}))
        )
        if str(intent.payload["reservation_reference"]) not in reservations:
            raise RuntimeError("warehouse management inventory reservation failed")


def _pick_and_require_consumption_request(
    run: _IntentExecution,
    engine,
    reconcile_shipped_egress: Callable[..., tuple[BoundaryMessage, ...]],
) -> None:
    persistence, fixtures, correlation_id = run.persistence, run.fixtures, run.correlation_id
    order = persistence.entity(
        "warehouse_fulfillment_order", fixtures.fulfillment.order_id,
    )
    if order is None:
        raise RuntimeError("composed fulfillment order disappeared")
    if order.state in {"allocated", "picking"}:
        fulfillment.pick_composed_order(
            persistence, engine, entities=fixtures.fulfillment,
        )
    # From picking, packed or shipped, the same durable pick occurrence
    # proves the WM request. On a replay it may also reconstruct an
    # already-durable dispatch message after WM has consumed stock.
    picked_messages = reconcile_shipped_egress(
        persistence, correlation_id=correlation_id,
        organization_owners={correlation_id: run.organization_id},
    )
    if not picked_messages or picked_messages[0].contract_key != (
        "warehouse.inventory_consumption_requested.v1"
    ):
        raise RuntimeError("durable composed picking must emit WM consumption")


def _pack_and_ship(run: _IntentExecution, engine) -> None:
    persistence, fixtures = run.persistence, run.fixtures
    order = persistence.entity(
        "warehouse_fulfillment_order", fixtures.fulfillment.order_id,
    )
    if order is not None and order.state == "picking":
        fulfillment.pack_order(
            persistence, engine, entities=fixtures.fulfillment,
        )
        order = persistence.entity(
            "warehouse_fulfillment_order", fixtures.fulfillment.order_id,
        )
    if order is not None and order.state == "packed":
        fulfillment.ship_order(
            persistence, engine, entities=fixtures.fulfillment,
        )
    elif order is None or order.state != "shipped":
        raise RuntimeError("composed fulfillment cannot resume terminal shipment")


def _execute_accept_inventory_reservation(
    run: _IntentExecution,
    *,
    reconcile_shipped_egress: Callable[..., tuple[BoundaryMessage, ...]],
) -> None:
    """`reconcile_shipped_egress` is the caller's `reconcile_shipped_fulfillment_egress`."""
    persistence, intent, fixtures = run.persistence, run.intent, run.fixtures
    _, engine = fulfillment.build_runtime(
        persistence, owner_id=run.organization_id, now=intent.due_at,
    )
    with _causal_command_scope(
        engine,
        intent=intent,
        correlation_id=run.correlation_id,
    ):
        if not fulfillment.allocate_composed_order(
            persistence,
            engine,
            entities=fixtures.fulfillment,
            stock_reference=str(intent.payload["stock_id"]),
            reservation_reference=str(intent.payload["reservation_reference"]),
            supplied_sku=str(intent.payload["sku"]),
            quantity=float(intent.payload["quantity"]),
        ):
            raise RuntimeError("composed fulfillment allocation failed")
        _pick_and_require_consumption_request(run, engine, reconcile_shipped_egress)
        _pack_and_ship(run, engine)


def _execute_consume_fulfillment_inventory(run: _IntentExecution) -> None:
    persistence, intent = run.persistence, run.intent
    _, engine = wm.build_runtime(persistence, owner_id=run.organization_id, now=intent.due_at)
    # False means the same reservation/consumption reference was already
    # durably applied; that is a successful idempotent retry after a crash.
    wm.consume_external_reservation(
        persistence,
        engine,
        stock_id=str(intent.payload["stock_id"]),
        quantity=float(intent.payload["quantity"]),
        sku=str(intent.payload["sku"]),
        reservation_reference=str(intent.payload["reservation_reference"]),
        consumption_reference=str(intent.payload["consumption_reference"]),
        caused_by=intent,
        correlation_id=run.correlation_id,
    )

"""PC6 ingress must create durable fulfillment from the received contract, not fixtures."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryService
from sose.composition.model import BoundaryMessage
from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.memory import MemoryPersistence

REFERENCE_OWNER_ID = "trading-customer-ingress-organization"


def _request(persistence, source_order_id, *, changes=None):
    target_id = deterministic_id(
        "entity", "warehouse_fulfillment_order",
        "warehouse-reference", f"order:{source_order_id}",
    )
    payload = {
        "order_id": source_order_id,
        "fulfillment_order_id": target_id,
        "requested_quantity": 10.0,
        "sku": "widget-a",
    }
    payload.update(changes or {})
    return Command(
        command_id=deterministic_id("ingress-test", source_order_id),
        name="composition.request_fulfillment_inventory",
        entity_type="warehouse_fulfillment_order",
        entity_id=target_id,
        due_at=o2c.ORIGIN,
        issued_at=o2c.ORIGIN,
        payload=payload,
    )


def test_payload_driven_fulfillment_creates_exactly_one_durable_order():
    persistence = MemoryPersistence()
    source = o2c.seed_reference(persistence, owner_id=REFERENCE_OWNER_ID)
    intent = _request(persistence, source.order_id)
    assert persistence.entity("warehouse_fulfillment_order", intent.entity_id) is None

    created = customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source.order_id, organization_id=REFERENCE_OWNER_ID,
    )
    assert created.order_id == intent.entity_id
    order = persistence.entity("warehouse_fulfillment_order", created.order_id)
    assert order is not None
    assert order.state == "requested"
    assert order.attributes["requested_quantity"] == 10.0
    assert order.attributes["requested_sku"] == "widget-a"
    assert order.attributes["inventory_owner"] == "warehouse_management"
    assert customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source.order_id, organization_id=REFERENCE_OWNER_ID,
    ) == created
    assert len(tuple(
        entity for entity in persistence.entities()
        if entity.entity_type == "warehouse_fulfillment_order"
    )) == 1


@pytest.mark.parametrize(
    "change,match",
    [
        ({"order_id": "foreign-sales-order"}, "source sales order mismatch"),
        ({"fulfillment_order_id": "foreign-fulfillment"}, "identity mismatch"),
        ({"requested_quantity": 0}, "invalid requested_quantity"),
        ({"requested_quantity": float("inf")}, "invalid requested_quantity"),
        ({"sku": ""}, "invalid sku"),
    ],
)
def test_payload_ingress_rejects_invalid_request_before_durable_creation(change, match):
    persistence = MemoryPersistence()
    source = o2c.seed_reference(persistence, owner_id=REFERENCE_OWNER_ID)
    intent = _request(persistence, source.order_id, changes=change)
    with pytest.raises(ValueError, match=match):
        customer._materialize_fulfillment_from_request(
            persistence, intent=intent, expected_sales_order_id=source.order_id, organization_id=REFERENCE_OWNER_ID,
        )
    assert not any(
        entity.entity_type == "warehouse_fulfillment_order"
        for entity in persistence.entities()
    )


def test_conflicting_replay_cannot_change_persisted_warehouse_order():
    persistence = MemoryPersistence()
    source = o2c.seed_reference(persistence, owner_id=REFERENCE_OWNER_ID)
    intent = _request(persistence, source.order_id)
    customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source.order_id, organization_id=REFERENCE_OWNER_ID,
    )
    conflict = _request(persistence, source.order_id, changes={"requested_quantity": 12.0})
    with pytest.raises(ValueError, match="conflicts with durable order"):
        customer._materialize_fulfillment_from_request(
            persistence, intent=conflict, expected_sales_order_id=source.order_id, organization_id=REFERENCE_OWNER_ID,
        )
    original = persistence.entity("warehouse_fulfillment_order", intent.entity_id)
    assert original.attributes["requested_quantity"] == 10.0


def test_customer_path_does_not_preseed_fulfillment_before_ingress(monkeypatch):
    original = customer.fulfillment.seed_composed_reference
    materializations = []

    def observed(persistence, **kwargs):
        request_key = kwargs["request_key"]
        order_id = deterministic_id(
            "entity", "warehouse_fulfillment_order", "warehouse-reference", request_key,
        )
        assert persistence.entity("warehouse_fulfillment_order", order_id) is None
        materializations.append(request_key)
        return original(persistence, **kwargs)

    monkeypatch.setattr(customer.fulfillment, "seed_composed_reference", observed)
    result = customer.run_customer_demand_path(organization_id="trading-customer-reference")
    assert materializations == [f"order:{result.o2c_order_id}"]
    persistence_order = result.persistence.entity(
        "warehouse_fulfillment_order", result.fulfillment_order_id
    )
    assert persistence_order is not None
    assert persistence_order.state == "shipped"


def test_payload_ingress_needs_no_producer_private_sales_order_record():
    persistence = MemoryPersistence()
    source_id = "external-o2c-order-0001"
    intent = _request(persistence, source_id)
    corr = deterministic_id("external-flow", source_id)
    envelope = BoundaryMessage.create(
        contract_name="o2c.fulfillment_requested",
        contract_version=1, source_domain="order_to_cash",
        source_identity=source_id, destination_domain="warehouse_fulfillment",
        occurrence_key="request-0001", correlation_id=corr,
        causation_id=None, produced_at=o2c.ORIGIN, payload=dict(intent.payload),
    )
    BoundaryService(persistence).publish(envelope)
    intent = replace(
        intent,
        causation_id=envelope.message_id,
        correlation_id=corr,
        payload={**intent.payload, "boundary_message_id": envelope.message_id},
    )
    assert persistence.entity("sales_order", source_id) is None
    created = customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source_id, organization_id=REFERENCE_OWNER_ID,
    )
    assert persistence.entity("warehouse_fulfillment_order", created.order_id)

    forged = replace(intent, payload={**intent.payload, "order_id": "foreign"})
    with pytest.raises(ValueError, match="source sales order mismatch"):
        customer._materialize_fulfillment_from_request(
            persistence, intent=forged, expected_sales_order_id=source_id, organization_id=REFERENCE_OWNER_ID,
        )
    wrong_source = replace(intent, causation_id="unregistered-message")
    with pytest.raises(ValueError, match="boundary source identity mismatch"):
        customer._materialize_fulfillment_from_request(
            persistence, intent=wrong_source, expected_sales_order_id=source_id, organization_id=REFERENCE_OWNER_ID,
        )


@pytest.mark.parametrize("request_kind", ["no-demand", "wrong-resource", "already-granted"])
def test_delivery_dispatch_without_pending_courier_demand_is_terminal(request_kind):
    """A failed business transition cannot be converted to a wait by a guessed ID."""
    from sose.core.runtime import ResourceDefinition, ResourceDemand, ResourceReservation
    from sose.examples.logistics import simulation as logistics

    persistence = MemoryPersistence()
    shipment_id = "courier-negative-shipment"
    request_id = f"delivery-courier:{logistics.delivery_attempt_id(1, shipment_id=shipment_id)}"
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("delivery_courier", capacity=1))
        if request_kind == "wrong-resource":
            uow.save_resource_definition(ResourceDefinition("other_resource", capacity=1))
            uow.save_resource_demand(ResourceDemand(
                request_id, "other_resource", 100, o2c.ORIGIN, 1,
            ))
        if request_kind == "already-granted":
            uow.save_resource_reservation(ResourceReservation(
                "courier-granted", request_id, "delivery_courier", o2c.ORIGIN, sequence=1,
            ))
    with pytest.raises(RuntimeError, match="logistics delivery dispatch failed"):
        customer._raise_delivery_dispatch_failure_or_wait(
            persistence, shipment_id=shipment_id, ordinal=1,
        )


def test_courier_dispatch_failure_has_authoritative_pending_demand():
    """The observed PC6 overlap leaves a durable demand, not a terminal failure."""
    from sose.core.runtime import ResourceDefinition, ResourceDemand
    from sose.examples.logistics import simulation as logistics

    persistence = MemoryPersistence()
    shipment_id = "courier-waiting-shipment"
    request_id = f"delivery-courier:{logistics.delivery_attempt_id(1, shipment_id=shipment_id)}"
    demand = ResourceDemand(request_id, "delivery_courier", 100, o2c.ORIGIN, 1)
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("delivery_courier", capacity=1))
        uow.save_resource_demand(demand)

    for _ in range(2):
        with pytest.raises(customer.DurableResourceWait) as pending:
            customer._raise_delivery_dispatch_failure_or_wait(
                persistence, shipment_id=shipment_id, ordinal=1,
            )
        assert (pending.value.resource_name, pending.value.request_id) == (
            "delivery_courier", request_id,
        )
    assert persistence.resource_demands() == (demand,)
    assert persistence.resource_reservations() == ()

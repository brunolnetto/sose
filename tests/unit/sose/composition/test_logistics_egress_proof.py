"""Contract falsification for durable Logistics egress proof, not mere ACK."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.model import BoundaryMessage
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 10, 9, 12)


def emit(
    service: BoundaryService, name: str, source: str, destination: str, identity: str,
    occurrence: str, payload: dict[str, str], correlation: str = "order-1",
):
    msg = BoundaryMessage.create(
        contract_name=name, contract_version=1,
        source_domain=source, source_identity=identity,
        destination_domain=destination, occurrence_key=occurrence,
        causation_id=None, correlation_id=correlation,
        produced_at=NOW, payload=payload,
    )
    service.publish(msg)
    return msg


def scenario(*, request_count: int = 1, state: str = "delivered",
             dispatch_ack: bool = True, pending_intent: bool = False):
    store = MemoryPersistence()
    service = BoundaryService(store)
    for i in range(request_count):
        emit(service, "o2c.fulfillment_requested", "order_to_cash",
             "warehouse_fulfillment", "sales-1", f"request-{i}",
             {"fulfillment_order_id": "wf-1", "order_id": "sales-1"})
    dispatch = emit(
        service, "warehouse.dispatch_ready", "warehouse_fulfillment", "logistics",
        "wf-1", "dispatch-ready",
        {"shipment_id": "shipment-1", "fulfillment_order_id": "wf-1"},
    )
    with store.transaction() as uow:
        uow.save_entity(Entity(id="shipment-1", entity_type="shipment", state=state))
    if dispatch_ack:
        registry = BoundaryConsumerRegistry()
        if pending_intent:
            registry.register(
                destination_domain="logistics", contract_name="warehouse.dispatch_ready",
                contract_version=1, handler=customer._intent_handler(
                    intent_name="composition.deliver_shipment",
                    entity_type="shipment", entity_id="shipment-1",
                ),
            )
        else:
            registry.register(
                destination_domain="logistics", contract_name="warehouse.dispatch_ready",
                contract_version=1, handler=lambda msg, uow: "effect-completed",
            )
        lease = service.claim_next(
            owner_id="worker", now=NOW, lease_duration=timedelta(minutes=5),
            destination_domain="logistics",
        )
        assert lease is not None
        service.consume(lease=lease, registry=registry, now=NOW)
    return store, dispatch


@pytest.mark.parametrize("state", ["created", "pickup_scheduled", "picked_up",
                                    "in_transfer", "out_for_delivery"])
def test_logistics_outbound_requires_delivered_state(state):
    store, _ = scenario(state=state)
    assert customer.reconcile_completed_logistics_egress(store) == ()


def test_ack_is_not_business_effect_and_later_checkpoint_unblocks():
    store, _ = scenario(pending_intent=True)
    assert customer.reconcile_completed_logistics_egress(store) == ()
    with store.transaction() as uow:
        consumed = next(
            uow.get_boundary_consumption(d.delivery_id)
            for d in uow.boundary_deliveries()
            if d.contract_name == "warehouse.dispatch_ready"
        )
        assert uow.get_command(consumed.consumer_effect_id)
        uow.delete_command(consumed.consumer_effect_id)
    published = customer.reconcile_completed_logistics_egress(store)
    assert len(published) == 1
    assert published[0].contract_key == "logistics.delivery_completed.v1"


def test_dispatch_claim_not_yet_acked_does_not_publish_outbound():
    store, _ = scenario(dispatch_ack=False)
    assert customer.reconcile_completed_logistics_egress(store) == ()


def test_dispatch_without_shipment_proof_is_not_published():
    store, _ = scenario()
    # A corrupted/missing state cannot be mistaken for successful delivery.
    with store.transaction() as uow:
        uow._working.entities.pop(("shipment", "shipment-1"))
    assert customer.reconcile_completed_logistics_egress(store) == ()


def test_missing_or_nonunique_order_cause_is_rejected():
    store, _ = scenario(request_count=0)
    with pytest.raises(RuntimeError, match="unique durable sales order cause"):
        customer.reconcile_completed_logistics_egress(store)
    store, _ = scenario(request_count=2)
    with pytest.raises(RuntimeError, match="unique durable sales order cause"):
        customer.reconcile_completed_logistics_egress(store)


def test_egress_is_bounded_retry_idempotent_and_correlation_scoped():
    store, dispatch = scenario()
    with pytest.raises(ValueError, match="max_new_messages"):
        customer.reconcile_completed_logistics_egress(store, max_new_messages=-1)
    assert customer.reconcile_completed_logistics_egress(store, correlation_id="other") == ()
    assert customer.reconcile_completed_logistics_egress(store, max_new_messages=0) == ()
    first = customer.reconcile_completed_logistics_egress(
        store, correlation_id=dispatch.correlation_id, max_new_messages=1,
    )
    assert len(first) == 1
    assert first[0].causation_id == dispatch.message_id
    assert first[0].payload()["order_id"] == "sales-1"
    assert customer.reconcile_completed_logistics_egress(store, max_new_messages=0) == first
    assert customer.reconcile_completed_logistics_egress(store, max_new_messages=1) == first


def test_existing_egress_identity_conflicting_with_durable_facts_is_rejected():
    store, dispatch = scenario()
    outgoing = customer.reconcile_completed_logistics_egress(store)[0]
    with store.transaction() as uow:
        # Simulate a corrupted historical egress identity from another writer.
        uow._working.boundary_messages[outgoing.message_id] = replace(
            outgoing, correlation_id="other-order",
        )
    with pytest.raises(ValueError, match="conflicts with source facts"):
        customer.reconcile_completed_logistics_egress(store)


def test_consumed_dispatch_without_consumption_receipt_is_not_a_valid_causal_proof():
    store, _ = scenario()
    with store.transaction() as uow:
        dispatch = next(
            d for d in uow.boundary_deliveries()
            if d.contract_name == "warehouse.dispatch_ready"
        )
        uow._working.boundary_consumptions.pop(dispatch.delivery_id)
    with pytest.raises(RuntimeError, match="missing consumption evidence"):
        customer.reconcile_completed_logistics_egress(store)

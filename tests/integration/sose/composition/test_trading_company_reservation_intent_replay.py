"""A consumed reservation intent may be retried after shipment and WM consumption."""
from __future__ import annotations

from sose.composition import trading_company_customer as customer
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.warehouse_fulfillment import simulation as fulfillment


def test_completed_reservation_intent_replay_remains_business_idempotent():
    run = customer.run_customer_demand_path()
    persistence = run.persistence
    with persistence.transaction() as uow:
        reservation_effect = next(
            uow.get_boundary_consumption(delivery.delivery_id).consumer_effect_id
            for delivery in uow.boundary_deliveries()
            if (
                (message := uow.get_boundary_message(delivery.message_id)) is not None
                and message.contract_key == "warehouse.inventory_reserved.v1"
            )
        )
        original_messages = tuple(
            (m.message_id, m.payload_hash)
            for d in uow.boundary_deliveries()
            if (m := uow.get_boundary_message(d.message_id)) is not None
        )

    before = tuple(
        (e.entity_type, e.id, e.state, e.version)
        for e in persistence.entities()
    )
    fixtures = customer._CustomerFixtures(
        o2c=o2c.O2CEntities(order_id=run.o2c_order_id),
        fulfillment=fulfillment.WarehouseEntities(
            order_id=run.fulfillment_order_id, lot_ids=(),
        ),
        warehouse=None, logistics=None, payments=None, r2r=None,
    )
    customer._execute_intent(
        persistence, effect_id=reservation_effect,
        fixtures=fixtures, correlation_id=run.correlation_id,
    )
    after = tuple(
        (e.entity_type, e.id, e.state, e.version)
        for e in persistence.entities()
    )
    assert after == before
    with persistence.transaction() as uow:
        replay_messages = tuple(
            (m.message_id, m.payload_hash)
            for d in uow.boundary_deliveries()
            if (m := uow.get_boundary_message(d.message_id)) is not None
        )
    assert replay_messages == original_messages

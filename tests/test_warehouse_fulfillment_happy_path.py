from sose.examples.warehouse_fulfillment.simulation import (
    PRIMARY_SKU,
    SUBSTITUTE_SKU,
    allocation_id,
    run_happy_path,
)


def test_order_allocates_primary_then_substitute_and_ships():
    persistence, entities = run_happy_path()

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    primary = persistence.entity("warehouse_inventory_lot", entities.lot_ids[0])
    substitute = persistence.entity("warehouse_inventory_lot", entities.lot_ids[1])
    primary_allocation = persistence.entity(
        "warehouse_allocation",
        allocation_id(order.id, primary.id),
    )
    substitute_allocation = persistence.entity(
        "warehouse_allocation",
        allocation_id(order.id, substitute.id),
    )

    assert order is not None and order.state == "shipped"
    assert primary is not None and substitute is not None
    assert primary.attributes["sku"] == PRIMARY_SKU
    assert substitute.attributes["sku"] == SUBSTITUTE_SKU
    assert primary.attributes["on_hand"] == 0.0
    assert substitute.attributes["on_hand"] == 1.0
    assert primary.attributes["allocated"] == 0.0
    assert substitute.attributes["allocated"] == 0.0
    assert primary_allocation is not None and primary_allocation.state == "shipped"
    assert substitute_allocation is not None and substitute_allocation.state == "shipped"
    assert primary_allocation.attributes["quantity"] == 6.0
    assert substitute_allocation.attributes["quantity"] == 4.0
    assert primary_allocation.attributes["substituted"] is False
    assert substitute_allocation.attributes["substituted"] is True
    assert len(order.attributes["occurrence_ids"]) == 4

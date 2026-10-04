from sose.examples.warehouse_fulfillment.entities import (
    Allocation,
    FulfillmentOrder,
    InventoryLot,
    InventoryOccurrence,
)
from sose.examples.warehouse_fulfillment.statecharts import (
    AllocationChart,
    FulfillmentOrderChart,
    InventoryOccurrenceChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_warehouse_entities_have_stable_types():
    assert FulfillmentOrder(id="o").entity_type == "warehouse_fulfillment_order"
    assert InventoryLot(id="l").entity_type == "warehouse_inventory_lot"
    assert Allocation(id="a").entity_type == "warehouse_allocation"
    assert InventoryOccurrence(id="e").entity_type == "warehouse_inventory_occurrence"


def test_allocation_projection_and_occurrence_history_are_separate():
    assert ("requested", "allocate", ("allocated",)) in _edges(FulfillmentOrderChart())
    assert ("allocated", "start_pick", ("picking",)) in _edges(FulfillmentOrderChart())
    assert ("committed", "pick", ("picked",)) in _edges(AllocationChart())
    assert ("picked", "ship", ("shipped",)) in _edges(AllocationChart())
    assert ("captured", "commit", ("committed",)) in _edges(InventoryOccurrenceChart())


def test_warehouse_transitions_are_orchestration_gated():
    for chart in (
        FulfillmentOrderChart(),
        AllocationChart(),
        InventoryOccurrenceChart(),
    ):
        assert policy_from_statechart(chart).events == ()

"""Reconstruction rejects missing pick evidence and inconsistent foreign ownership."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from sose.composition import trading_company_customer as customer


class _ReadFault:
    """Read-only fault overlay: never mutates the underlying durable store."""

    def __init__(self, underlying, case: str):
        self.underlying = underlying
        self.case = case

    def __getattr__(self, name):
        return getattr(self.underlying, name)

    def entities(self):
        entities = tuple(self.underlying.entities())
        if self.case == "ambiguous_shipment":
            return (*entities, SimpleNamespace(entity_type="shipment", id="other-shipment"))
        return entities

    def entity(self, entity_type, entity_id):
        real = self.underlying.entity(entity_type, entity_id)
        if real is None:
            return None
        if self.case == "missing_pick" and entity_type == "warehouse_inventory_occurrence":
            return None
        if entity_type not in {"warehouse_fulfillment_order", "warehouse_allocation"}:
            return real
        entity = deepcopy(real)
        if entity_type == "warehouse_fulfillment_order":
            if self.case == "not_picked":
                entity.state = "allocated"
            elif self.case in {"unpicked_allocation", "missing_pick"}:
                entity.state = "picking"
            elif self.case == "missing_allocation":
                entity.attributes["allocation_ids"] = []
            elif self.case == "wrong_quantity":
                entity.attributes["requested_quantity"] = 11.0
        if entity_type == "warehouse_allocation":
            if self.case == "unpicked_allocation":
                entity.state = "committed"
            elif self.case == "foreign_allocation":
                entity.attributes["stock_reference"] = "wrong-stock"
        return entity


@pytest.mark.parametrize(
    "case,error",
    [
        ("not_picked", None),
        ("unpicked_allocation", None),
        ("missing_pick", None),
        ("foreign_allocation", None),
        ("missing_allocation", "one durable WM allocation"),
        ("ambiguous_shipment", "shipment linkage is ambiguous"),
        ("wrong_quantity", "reservation does not reconcile"),
    ],
)
def test_recovery_never_invents_missing_or_conflicting_pick_evidence(case, error):
    executed = customer.run_customer_demand_path()
    overlay = _ReadFault(executed.persistence, case)
    if error is None:
        assert customer.reconcile_shipped_fulfillment_egress(
            overlay, correlation_id=executed.correlation_id,
        ) == ()
    else:
        with pytest.raises(ValueError, match=error):
            customer.reconcile_shipped_fulfillment_egress(
                overlay, correlation_id=executed.correlation_id,
            )
    # The injected read view cannot alter authoritative operational truth.
    assert executed.persistence.entity(
        "warehouse_fulfillment_order", executed.fulfillment_order_id,
    ).state == "shipped"

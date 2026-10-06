from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


def _backend(origin):
    return SimPyBackend(origin=origin)


def _job(domain: str, job_id: str) -> SimulationJob:
    return SimulationJob(
        job_id=job_id,
        definition=builtin_catalog().get(domain),
        persistence=MemoryPersistence(),
        backend_factory=_backend,
    )


@pytest.mark.parametrize(
    "domain",
    [
        "cards_payments",
        "logistics",
        "telecom",
        "warehouse_fulfillment",
        "warehouse_management",
        "subscription_saas",
    ],
)
def test_operational_domains_expose_recurring_reconcile_hook(domain):
    assert builtin_catalog().get(domain).reconcile_tick is not None


def test_cards_payment_recurring_job_waits_for_settlement_boundary():
    job = _job("cards_payments", "cards-recurring")
    state = job.initialize({"settlement_delay": timedelta(hours=2)})

    first = job.run_tick(trigger_id="cards-1")
    payment = job.persistence.entity("card_payment", state.bootstrap_state.payment_id)
    assert first.logical_tick == 1
    assert payment.state == "captured"

    job.run_tick(trigger_id="cards-2")
    payment = job.persistence.entity("card_payment", state.bootstrap_state.payment_id)
    assert payment.state == "captured"

    third = job.run_tick(trigger_id="cards-3")
    payment = job.persistence.entity("card_payment", state.bootstrap_state.payment_id)
    assert third.logical_tick == 3
    assert payment.state == "settled"


def test_logistics_recurring_job_reconciles_eligible_flow_after_pickup_due():
    job = _job("logistics", "logistics-recurring")
    state = job.initialize({"pickup_delay": timedelta(hours=1)})

    result = job.run_tick(trigger_id="logistics-1")
    shipment = job.persistence.entity("shipment", state.bootstrap_state.shipment_id)

    assert result.logical_tick == 1
    assert shipment.state == "delivered"


def test_telecom_recurring_job_preserves_activation_delay_across_ticks():
    job = _job("telecom", "telecom-recurring")
    state = job.initialize({"activation_delay": timedelta(hours=2)})

    job.run_tick(trigger_id="telecom-1")
    order = job.persistence.entity(
        "telecom_product_order",
        state.bootstrap_state.product_order_id,
    )
    assert order.state == "in_progress"

    job.run_tick(trigger_id="telecom-2")
    order = job.persistence.entity(
        "telecom_product_order",
        state.bootstrap_state.product_order_id,
    )
    assert order.state == "in_progress"

    third = job.run_tick(trigger_id="telecom-3")
    order = job.persistence.entity(
        "telecom_product_order",
        state.bootstrap_state.product_order_id,
    )
    assert third.logical_tick == 3
    assert order.state == "completed"


def test_warehouse_recurring_job_reconciles_available_inventory_to_shipment():
    job = _job("warehouse_fulfillment", "warehouse-recurring")
    state = job.initialize()

    result = job.run_tick(trigger_id="warehouse-1")
    order = job.persistence.entity(
        "warehouse_fulfillment_order",
        state.bootstrap_state.order_id,
    )

    assert result.logical_tick == 1
    assert order.state == "shipped"


def test_warehouse_management_recurring_job_progresses_physical_transfer():
    job = _job("warehouse_management", "warehouse-management-recurring")
    state = job.initialize()

    for index in range(4):
        job.run_tick(trigger_id=f"warehouse-management-{index + 1}")

    shipment = job.persistence.entity(
        "warehouse_management_shipment",
        state.bootstrap_state.shipment_id,
    )
    destination_stock = job.persistence.entity(
        "warehouse_management_stock",
        state.bootstrap_state.destination_stock_id,
    )

    assert shipment.state == "completed"
    assert destination_stock.attributes["on_hand"] == pytest.approx(8.0)


def test_subscription_recurring_job_applies_future_plan_change_on_tick_boundary():
    job = _job("subscription_saas", "subscription-recurring")
    state = job.initialize(
        {
            "target_plan": "pro",
            "plan_change_after": timedelta(hours=2),
        }
    )

    first = job.run_tick(trigger_id="subscription-1")
    subscription = job.persistence.entity(
        "saas_subscription",
        state.bootstrap_state.subscription_id,
    )
    assert first.logical_tick == 1
    assert subscription.attributes["plan_code"] == "basic"
    assert subscription.attributes["change_request_ids"]

    second = job.run_tick(trigger_id="subscription-2")
    subscription = job.persistence.entity(
        "saas_subscription",
        state.bootstrap_state.subscription_id,
    )

    assert second.logical_tick == 2
    assert subscription.attributes["plan_code"] == "pro"

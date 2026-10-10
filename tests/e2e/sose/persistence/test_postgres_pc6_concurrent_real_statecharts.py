"""Two real PC6 domain workflows contend in one PostgreSQL namespace.

The first durable ingress execution is barrier-synchronized: the test is not
two sequential completions on two independent database connections.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier, Lock
from uuid import uuid4
import json
import os

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.audit import audit_causal_history
from sose.composition.bindings import SettlementBindingService
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


def _canonical(values):
    return tuple(sorted(json.dumps(
        asdict(value), sort_keys=True, default=str, separators=(",", ":"),
    ) for value in values))


def test_pg_two_real_customer_statecharts_overlap_without_business_aliasing(monkeypatch):
    assert DSN is not None
    namespace = "pc6_parallel_" + uuid4().hex[:12]
    arrival = Barrier(2)
    lock = Lock()
    ingress_correlations = []
    original = customer._execute_intent

    def synchronized_first_ingress(persistence, *, effect_id, fixtures, correlation_id):
        command = persistence.command(effect_id)
        if command is not None and command.name == "composition.request_fulfillment_inventory":
            with lock:
                ingress_correlations.append(correlation_id)
            # Both workers have durably ACKed their independent v2 ingress.
            # Neither can execute its first domain intent before the other.
            arrival.wait(timeout=20)
        return original(
            persistence, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id,
        )

    monkeypatch.setattr(customer, "_execute_intent", synchronized_first_ingress)

    def worker(key):
        with PostgresPersistence(DSN, namespace=namespace) as persistence:
            return customer.run_customer_demand_path(
                persistence=persistence, instance_key=key,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, key) for key in ("customer-a", "customer-b")]
        results = tuple(f.result(timeout=90) for f in futures)

    assert len(ingress_correlations) == 2
    assert len(set(ingress_correlations)) == 2
    assert len({r.o2c_order_id for r in results}) == 2
    assert len({r.payment_id for r in results}) == 2
    assert len({r.journal_id for r in results}) == 2
    assert len({r.shipment_id for r in results}) == 2
    assert len({r.warehouse_stock_id for r in results}) == 2
    assert len(set(results[0].message_ids + results[1].message_ids)) == 16
    assert len(set(results[0].effect_ids + results[1].effect_ids)) == 16

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        report = audit_causal_history(recovered)
        assert report.message_count == 16
        assert report.applied_effects == 8
        assert report.pending_effects == 0
        assert len(recovered.business_effects()) == 8
        events = recovered.events()
        assert len(events) == len({event.event_id for event in events})
        for flow in results:
            for kind, entity_id, expected in (
                ("sales_order", flow.o2c_order_id, "invoiced"),
                ("shipment", flow.shipment_id, "delivered"),
                ("card_payment", flow.payment_id, "settled"),
                ("journal_entry", flow.journal_id, "posted"),
            ):
                entity = recovered.entity(kind, entity_id)
                assert entity is not None and entity.state == expected
                assert any(
                    event.entity_type == kind and event.entity_id == entity_id
                    for event in events
                ), (kind, entity_id, "missing independently attributed domain event")
            binding = SettlementBindingService(recovered).for_order(flow.o2c_order_id)
            assert binding is not None
            assert binding.payment_id == flow.payment_id
            assert binding.journal_id == flow.journal_id
            assert SettlementBindingService(recovered).for_payment(flow.payment_id) == binding
            assert SettlementBindingService(recovered).for_journal(flow.journal_id) == binding

        with recovered.boundary_transaction() as uow:
            deliveries = uow.boundary_deliveries()
            assert len(deliveries) == 16
            assert all(
                uow.get_boundary_consumption(d.delivery_id) is not None
                for d in deliveries
            )
        assert all(
            recovered.command(receipt.effect_id) is None
            for receipt in recovered.business_effects()
        )
        initial = (
            report,
            _canonical(recovered.entities()),
            _canonical(events),
            _canonical(recovered.business_effects()),
            _canonical(recovered.resource_reservations()),
            _canonical(recovered.scheduled_work()),
            _canonical(recovered.store_items()),
            _canonical(recovered.container_states()),
            _canonical(recovered.job_states()),
            _canonical(recovered.sink_checkpoints()),
        )

    with PostgresPersistence(DSN, namespace=namespace) as reopened:
        assert initial == (
            audit_causal_history(reopened),
            _canonical(reopened.entities()),
            _canonical(reopened.events()),
            _canonical(reopened.business_effects()),
            _canonical(reopened.resource_reservations()),
            _canonical(reopened.scheduled_work()),
            _canonical(reopened.store_items()),
            _canonical(reopened.container_states()),
            _canonical(reopened.job_states()),
            _canonical(reopened.sink_checkpoints()),
        )

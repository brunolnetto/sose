"""Full PC6 domain-engine equivalence across independent PostgreSQL reconnect.

Unlike the boundary-only #426 protocol fixture, no terminal state is manually
fabricated here: the actual six domain statecharts run the customer paths.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from sose.composition.audit import audit_causal_history
from sose.composition.bindings import SettlementBindingService
from sose.composition.trading_company_customer import run_customer_demand_path
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")


def _canonical(values):
    return tuple(sorted(
        json.dumps(asdict(v), sort_keys=True, separators=(",", ":"), default=str)
        for v in values
    ))


def _view(store, results):
    audit = audit_causal_history(store)
    with store.boundary_transaction() as uow:
        deliveries = uow.boundary_deliveries()
        boundaries = tuple(sorted(
            (d.message_id, d.delivery_id, d.status.value,
             uow.get_boundary_consumption(d.delivery_id).consumer_effect_id)
            for d in deliveries
        ))
    return {
        "audit": audit.semantic_digest,
        "typed_edges": audit.typed_edges,
        "applied_effects": audit.applied_effects,
        "pending_effects": audit.pending_effects,
        "entities": _canonical(store.entities()),
        "events": _canonical(store.events()),
        "certificates": _canonical(store.business_effects()),
        "boundaries": boundaries,
        "bindings": _canonical(
            SettlementBindingService(store).for_order(x.o2c_order_id)
            for x in results
        ),
        "resource_reservations": _canonical(store.resource_reservations()),
        "scheduled": _canonical(store.scheduled_work()),
        "store_items": _canonical(store.store_items()),
        "container_states": _canonical(store.container_states()),
        "job_states": _canonical(store.job_states()),
        "sink_checkpoints": _canonical(store.sink_checkpoints()),
    }


def _reconnected(namespace):
    with PostgresPersistence(DSN, namespace=namespace) as store:
        first = run_customer_demand_path(
            persistence=store, instance_key="customer-a",
        )
    # Genuine connection disposal and rehydration, not only new in-memory
    # runtime objects. The second real path sees authoritative PostgreSQL truth.
    with PostgresPersistence(DSN, namespace=namespace) as store:
        second = run_customer_demand_path(
            persistence=store, instance_key="customer-b",
        )
    with PostgresPersistence(DSN, namespace=namespace) as store:
        return _view(store, (first, second))


def _continuous(namespace):
    with PostgresPersistence(DSN, namespace=namespace) as store:
        first = run_customer_demand_path(persistence=store, instance_key="customer-a")
        second = run_customer_demand_path(persistence=store, instance_key="customer-b")
        assert first.o2c_order_id != second.o2c_order_id
        return _view(store, (first, second))


def test_pg_two_full_pc6_customer_paths_match_reconnected_control(tmp_path):
    assert DSN is not None
    continuous = _continuous("pc6_full_base_" + uuid4().hex[:12])
    recovered = _reconnected("pc6_full_rec_" + uuid4().hex[:12])
    destination = Path(os.environ.get(
        "SOSE_PC6_REAL_STATECHART_REPORT", str(tmp_path / "pc6-real-statecharts.json"),
    ))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(
        {"protocol": "pc6-real-domain-paths-v1", "continuous": continuous,
         "reconnected": recovered, "equivalent": continuous == recovered},
        sort_keys=True, indent=2, default=str,
    ), encoding="utf-8")
    assert continuous["audit"] == recovered["audit"]
    assert continuous["applied_effects"] == recovered["applied_effects"] == 8
    assert continuous["pending_effects"] == recovered["pending_effects"] == 0
    assert len(continuous["events"]) == len(recovered["events"])
    assert recovered == continuous

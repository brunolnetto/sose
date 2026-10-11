"""Full real PC6 statecharts through a durable recurring scheduler after OS death.

No reference terminal entities: real Logistics, O2C, Payments, R2R are
executed through the production composition recovery runner.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.audit import audit_causal_history
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
POLICY = "composition.deliver_shipment"
INTERVAL = timedelta(hours=1)
CAPACITY = ResourcePoolContract(
    resource_type="pickup_courier", pool_id="pc6-real-all-domains",
    scope="shared", capacity=1,
)


def _namespace(prefix):
    return prefix + "_" + uuid4().hex[:12]


def _bootstrap(dsn, namespace, flow, *, kill=False):
    original = customer._execute_intent

    def stop_after_real_logistics_ack(store, *, effect_id, fixtures, correlation_id):
        command = store.command(effect_id)
        if command is not None and command.name == POLICY:
            # The real warehouse / inventory and boundary ACK transactions
            # have already completed. The Logistics Command has not run.
            assert any(
                receipt.consumer_effect_id == effect_id
                for receipt in _consumptions(store)
            )
            if kill:
                os._exit(79)
            raise RuntimeError("PC6 bootstrap handoff to recurring worker")
        return original(
            store, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id,
        )

    customer._execute_intent = stop_after_real_logistics_ack
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            if kill:
                customer.run_customer_demand_path(
                    persistence=store, instance_key=flow,
                )
                raise AssertionError("child survived the ACK cut")
            with pytest.raises(RuntimeError, match="bootstrap handoff"):
                customer.run_customer_demand_path(
                    persistence=store, instance_key=flow,
                )
    finally:
        customer._execute_intent = original


def _consumptions(store):
    with store.boundary_transaction() as uow:
        return tuple(
            receipt
            for delivery in uow.boundary_deliveries()
            if (receipt := uow.get_boundary_consumption(delivery.delivery_id)) is not None
        )


def _schedule(domain, shared, flow, *, owner):
    # Bound one physical ledger, separate durable jobs/writers and clocks.
    return RecoverySchedule(
        TradingCustomerRecoveryRunner(
            persistence=domain, owner_id=owner, job_id="pc6-" + flow,
            max_actions=1, resource_persistence=shared,
            resource_policies={POLICY: CAPACITY},
            resource_organizations={
                _org_correlation(domain): flow,
            },
            resource_slot_duration=timedelta(minutes=5),
            resource_retry_delay=timedelta(minutes=5),
        ),
        start_at=o2c.ORIGIN, interval=INTERVAL, max_slots=1,
    )


def _org_correlation(store):
    with store.boundary_transaction() as uow:
        deliveries = uow.boundary_deliveries()
        ids = {
            m.correlation_id for d in deliveries
            if (m := uow.get_boundary_message(d.message_id)) is not None
        }
    assert len(ids) == 1
    return next(iter(ids))


def _step(dsn, names, physical, flow, index):
    with PostgresPersistence(dsn, namespace=names[flow]) as org:
        with PostgresPersistence(dsn, namespace=physical) as shared:
            tick = _schedule(org, shared, flow, owner=f"worker-{flow}-{index}")
            ticks = tick.run_due(now=o2c.ORIGIN + index * INTERVAL)
            assert len(ticks) <= 1
            return len(org.business_effects()), org.job_state("pc6-" + flow)


def _view(dsn, namespace, flow):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        audit = audit_causal_history(store)
        def canonical(records):
            return tuple(sorted(json.dumps(
                asdict(item), sort_keys=True, default=str,
                separators=(",", ":"),
            ) for item in records))
        with store.boundary_transaction() as uow:
            deliveries = uow.boundary_deliveries()
            messages = tuple(
                uow.get_boundary_message(d.message_id) for d in deliveries
            )
            consumptions = tuple(
                uow.get_boundary_consumption(d.delivery_id) for d in deliveries
            )
        finals = {
            k: tuple(e.state for e in store.entities() if e.entity_type == k)
            for k in ("shipment", "sales_order", "card_payment", "journal_entry")
        }
        job = store.job_state("pc6-" + flow)
        assert job is not None and job.active_trigger_id is None
        assert finals["shipment"] == ("delivered",)
        assert finals["sales_order"] == ("invoiced",)
        assert finals["card_payment"] == ("settled",)
        assert finals["journal_entry"] == ("posted",)
        assert len(store.business_effects()) >= 4
        return {
            "audit": audit.semantic_digest,
            "typed_edges": audit.typed_edges,
            "entities": canonical(store.entities()),
            "events": canonical(store.events()),
            "messages": canonical(messages),
            "consumptions": canonical(x for x in consumptions if x is not None),
            "certificates": canonical(store.business_effects()),
            "scheduled": canonical(store.scheduled_work()),
            "resources": canonical(store.resource_reservations()),
            "stores": canonical(store.store_items()),
            "containers": canonical(store.container_states()),
            "job_triggers": tuple(
                (x.trigger_id, x.logical_time, x.end_tick)
                for x in job.completed_batch_triggers
            ),
            "job_time": job.logical_time,
            "position": store.simulation_position(),
        }


def _experiment(dsn, *, kill):
    names = {flow: _namespace("pc6full" + flow) for flow in ("a", "b")}
    physical = _namespace("pc6fullpool")
    if kill:
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             "--pc6-real-worker", dsn, names["a"], "a"],
            capture_output=True, text=True, check=False, timeout=90,
        )
        assert child.returncode == 79, child.stderr[-3000:]
    else:
        _bootstrap(dsn, names["a"], "a")
    _bootstrap(dsn, names["b"], "b")

    # Let B advance on its own schedule while A's worker is absent.
    for tick in range(36):
        _step(dsn, names, physical, "b", tick)
    for tick in range(36):
        _step(dsn, names, physical, "a", tick)

    views = {flow: _view(dsn, names[flow], flow) for flow in ("a", "b")}
    # Physical ledger UUIDs embed operational namespaces. Prove no physical
    # double booking independently, without erasing any business semantics.
    with PostgresPersistence(dsn, namespace=physical) as shared:
        ledger = shared.temporal_resources()
        assert ledger.audit()
        snapshot = ledger.snapshot()
        assert len(snapshot["reservations"]) == 2
        clocks = {
            flow: shared._connection.execute(
                f'SELECT logical_time FROM "{physical}_resource_org_position" '
                "WHERE organization_id = %s", (flow,),
            ).fetchone()[0] for flow in ("a", "b")
        }
    return views, clocks


def test_pg_real_four_domain_customer_statecharts_recover_after_os_kill(tmp_path):
    assert DSN is not None
    continuous, reference_clocks = _experiment(DSN, kill=False)
    recovered, resumed_clocks = _experiment(DSN, kill=True)
    assert recovered == continuous
    assert resumed_clocks == reference_clocks
    evidence = {
        "protocol": "pc6-real-multidomain-recurring-v1",
        "fault": "os._exit(79) after real Logistics ACK+Command, before execution",
        "domains": ["Logistics", "O2C", "Cards Payments", "R2R"],
        "organizations": ["a", "b"],
        "full_business_equivalence": True,
        "reference_sha256": sha256(json.dumps(
            continuous, sort_keys=True, default=str,
        ).encode()).hexdigest(),
        "recovered_sha256": sha256(json.dumps(
            recovered, sort_keys=True, default=str,
        ).encode()).hexdigest(),
    }
    report = Path(os.environ.get(
        "SOSE_PC6_REAL_MULTIDOMAIN_REPORT",
        str(tmp_path / "pc6-real-multidomain-recurring.json"),
    ))
    report.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--pc6-real-worker":
    _bootstrap(sys.argv[2], sys.argv[3], sys.argv[4], kill=True)

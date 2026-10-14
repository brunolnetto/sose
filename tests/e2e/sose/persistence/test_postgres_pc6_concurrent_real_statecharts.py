"""Two real PC6 domain workflows contend in one PostgreSQL namespace.

The first durable ingress execution is barrier-synchronized: the test is not
two sequential completions on two independent database connections.
"""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict
import traceback
from threading import Barrier, Event, Lock, Thread
from uuid import uuid4
import json
import os
from pathlib import Path

import pytest
import psycopg
from psycopg import sql

from sose.composition import trading_company_customer as customer
from sose.composition.audit import audit_causal_history
from sose.composition.bindings import SettlementBindingService
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.records import StateRecord, records_to_state
from scripts.reproduce_pc6_pg_overlap import capture_first_worker_failure_once

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


def _canonical(values):
    return tuple(sorted(json.dumps(
        asdict(value), sort_keys=True, default=str, separators=(",", ":"),
    ) for value in values))


def _read_first_failure_snapshot(dsn: str, namespace: str) -> dict[str, object]:
    """Collect one committed MVCC snapshot without constructing/migrating persistence.

    SQL is read-only and introspects only an already existing namespace record
    table; a failed bootstrap remains failed instead of being reinitialized.
    """
    try:
        # Forensic reads must be bounded independently of the production
        # workers. Never hold the original exception hostage to a table lock.
        with psycopg.connect(
            dsn,
            autocommit=True,
            connect_timeout=3,
            options="-c lock_timeout=1500 -c statement_timeout=3000",
        ) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                table_name = namespace + "_record"
                found = connection.execute(
                    "SELECT to_regclass(%s)", (table_name,),
                ).fetchone()
                if found is None or found[0] is None:
                    return {"namespace": namespace, "record_table_missing": True}
                rows = connection.execute(
                    sql.SQL(
                        "SELECT collection, record_key, position, payload "
                        "FROM {} ORDER BY collection, position, record_key"
                    ).format(sql.Identifier(table_name)),
                ).fetchall()
                state = records_to_state(
                    StateRecord(
                        collection=str(row[0]), key=str(row[1]),
                        position=int(row[2]), payload=str(row[3]),
                    )
                    for row in rows
                )
                identity_sample: list[tuple[str, str, int]] = []
                counts: dict[str, int] = {}
                for collection, record_key, record_position, _payload in rows:
                    key = str(collection)
                    if counts.get(key, 0) < 5:
                        identity_sample.append((key, str(record_key), int(record_position)))
                    counts[key] = counts.get(key, 0) + 1
                positions = state.simulation_positions
                events = state.events
                return {
                    "namespace": namespace,
                    "record_identities": identity_sample,
                    "record_identity_count": len(rows),
                    "positions": {
                        owner: asdict(position)
                        for owner, position in sorted(positions.items())
                    },
                    "entities": sorted(
                        (entity.entity_type, entity.id, entity.state, entity.version)
                        for entity in state.entities.values()
                    )[:40],
                    "event_count": len(events),
                    "events": sorted(
                        (e.event_id, e.entity_type, e.entity_id, e.correlation_id)
                        for e in events
                    )[:40],
                    "resource_reservations": sorted((
                        r.reservation_id, r.resource_name, r.request_id,
                    ) for r in state.resource_reservations.values())[:30],
                    "store_items": sorted((
                        item.store_name, item.item_id,
                    ) for item in state.store_items.values())[:30],
                    "store_get_requests": sorted((
                        request.store_name, request.request_id,
                    ) for request in state.store_get_requests.values())[:30],
                    "store_get_results": sorted((
                        result.request_id, result.item.item_id,
                    ) for result in state.store_get_results.values())[:30],
                    "scheduled_work": sorted((
                        work.work_id, str(work.due_at),
                    ) for work in state.scheduled_work.values())[:30],
                    "job_states": sorted((
                        job.job_id, job.run_count,
                    ) for job in state.job_states.values())[:30],
                    "business_effects": sorted(state.business_effects)[:30],
                }
    except Exception as exc:
        return {
            "namespace": namespace, "snapshot_error": repr(exc),
            "traceback": "".join(traceback.format_exception(exc))[-4000:],
        }


@contextmanager
def _forensic_on_failure(dsn: str, namespace: str):
    """Annotate every post-worker assertion with a read-only committed snapshot."""
    try:
        yield
    except Exception as exc:
        exc.add_note(
            "Post-worker committed PostgreSQL forensic snapshot: "
            + json.dumps(
                _read_first_failure_snapshot(dsn, namespace),
                default=str, sort_keys=True,
            )[:18000]
        )
        raise

def test_pg_two_real_customer_statecharts_overlap_without_business_aliasing(monkeypatch):
    assert DSN is not None
    namespace = "pc6_parallel_" + uuid4().hex[:12]
    arrival = Barrier(2)
    lock = Lock()
    ingress_correlations = []
    first_worker_failure = []
    forensic_lock = Lock()
    original = customer._execute_intent

    def synchronized_first_ingress(persistence, *, effect_id, fixtures, correlation_id, organization_id):
        command = persistence.command(effect_id)
        if command is not None and command.name == "composition.request_fulfillment_inventory":
            with lock:
                ingress_correlations.append(correlation_id)
            # Both workers have durably ACKed their independent v2 ingress.
            # Neither can execute its first domain intent before the other.
            arrival.wait(timeout=7)
        return original(
            persistence, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id, organization_id=organization_id,
        )

    monkeypatch.setattr(customer, "_execute_intent", synchronized_first_ingress)

    def worker(key):
        try:
            with PostgresPersistence(DSN, namespace=namespace) as persistence:
                return customer.run_customer_demand_path(
                    organization_id={"customer-a": "org-a", "customer-b": "org-b"}[key],
                    persistence=persistence, instance_key=key,
                )
        except Exception as error:
            # The failure happens before the surviving peer finishes. Retain
            # the earliest committed MVCC read immediately from this path.
            capture_first_worker_failure_once(
                first_worker_failure, forensic_lock,
                flow=key, error=error,
                snapshot=lambda: _read_first_failure_snapshot(DSN, namespace),
                evidence_path=(
                    Path(os.environ["SOSE_PC6_FIRST_FAILURE_SNAPSHOT"])
                    if os.environ.get("SOSE_PC6_FIRST_FAILURE_SNAPSHOT") else None
                ),
            )
            raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, key) for key in ("customer-a", "customer-b")]
        finished, unfinished = wait(futures, timeout=35)
        assert not unfinished, (
            "worker did not complete within bounded interval; first failure: "
            + json.dumps(first_worker_failure[0] if first_worker_failure else None,
                         default=str, sort_keys=True)[:18000]
        )
        failures = [
            {
                "flow": key,
                "error": repr(future.exception()),
                "traceback": "".join(traceback.format_exception(future.exception()))[-4000:],
            }
            for key, future in zip(("customer-a", "customer-b"), futures)
            if future.exception() is not None
        ]
        assert not failures, (
            "independent worker errors; committed PostgreSQL forensic snapshot follows: "
            + json.dumps(
                {
                    "failures": failures,
                    "first_failure": first_worker_failure[0] if first_worker_failure else None,
                    "post_worker_snapshot": _read_first_failure_snapshot(DSN, namespace),
                },
                default=str, sort_keys=True,
            )[:18000]
        )
        results = tuple(f.result() for f in futures)

    with _forensic_on_failure(DSN, namespace):
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


def test_pg_concurrent_first_namespace_bootstrap_is_serializable():
    """Four fresh writers may create the same namespace without catalog races."""
    assert DSN is not None
    namespace = "pc6_boot_" + uuid4().hex[:12]
    gate = Barrier(4)

    def open_writer(index):
        gate.wait(timeout=10)
        with PostgresPersistence(DSN, namespace=namespace) as store:
            assert store.persisted_record_count() == 0
            return store.writer_epoch()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(open_writer, i) for i in range(4)]
        finished, unfinished = wait(futures, timeout=30)
        assert not unfinished, "concurrent PostgreSQL bootstrap timed out"
        errors = [repr(f.exception()) for f in futures if f.exception() is not None]
        assert not errors, f"concurrent bootstrap violated catalog uniqueness: {errors}"
        epochs = tuple(f.result() for f in futures)
    assert len(set(epochs)) == 1

    with PostgresPersistence(DSN, namespace=namespace) as restored:
        assert restored.persisted_record_count() == 0
        assert restored.writer_epoch() == epochs[0]


def test_pg_business_resource_guard_serializes_independent_workers():
    """The shared journal capacity guard must span inner domain transactions."""
    assert DSN is not None
    namespace = "pc6_guard_" + uuid4().hex[:12]
    with (
        PostgresPersistence(DSN, namespace=namespace) as first,
        PostgresPersistence(DSN, namespace=namespace) as second,
    ):
        acquired_first = Event()
        release_first = Event()
        attempted_second = Event()
        acquired_second = Event()
        failures = []

        def owner():
            try:
                with first.business_resource_guard("r2r.posting_processor"):
                    acquired_first.set()
                    if not release_first.wait(10):
                        raise TimeoutError("first posting worker was not released")
            except BaseException as error:
                failures.append(error)

        def contender():
            attempted_second.set()
            try:
                with second.business_resource_guard("r2r.posting_processor"):
                    acquired_second.set()
            except BaseException as error:
                failures.append(error)

        a = Thread(target=owner)
        b = Thread(target=contender)
        a.start()
        assert acquired_first.wait(10)
        b.start()
        assert attempted_second.wait(10)
        try:
            assert not acquired_second.wait(0.3)
        finally:
            release_first.set()
        a.join(timeout=10)
        b.join(timeout=10)
        assert not a.is_alive() and not b.is_alive()
        assert acquired_second.is_set()
        assert failures == []


def test_pg_cards_resource_guard_serializes_independent_workers():
    """The shared payment processor capacity guard must span inner domain transactions."""
    assert DSN is not None
    namespace = "pc6_guard_" + uuid4().hex[:12]
    with (
        PostgresPersistence(DSN, namespace=namespace) as first,
        PostgresPersistence(DSN, namespace=namespace) as second,
    ):
        acquired_first = Event()
        release_first = Event()
        attempted_second = Event()
        acquired_second = Event()
        failures = []

        def owner():
            try:
                with first.business_resource_guard("cards_payments.authorization_settlement"):
                    acquired_first.set()
                    if not release_first.wait(10):
                        raise TimeoutError("first payment worker was not released")
            except BaseException as error:
                failures.append(error)

        def contender():
            attempted_second.set()
            try:
                with second.business_resource_guard("cards_payments.authorization_settlement"):
                    acquired_second.set()
            except BaseException as error:
                failures.append(error)

        a = Thread(target=owner)
        b = Thread(target=contender)
        a.start()
        assert acquired_first.wait(10)
        b.start()
        assert attempted_second.wait(10)
        try:
            assert not acquired_second.wait(0.3)
        finally:
            release_first.set()
        a.join(timeout=10)
        b.join(timeout=10)
        assert not a.is_alive() and not b.is_alive()
        assert acquired_second.is_set()
        assert failures == []

"""PC6 falsifier: independent recurring jobs, one physical pool, real worker death.

The child exits via os._exit after the real Logistics statechart and immutable
business certificate commit but before the shared ledger booking is released.
Recovery uses fresh database connections and no in-process stage history.
"""
from __future__ import annotations

import os
import subprocess
import sys
from threading import Event, Thread
from datetime import timedelta
from uuid import uuid4

import pytest

from sose.composition.boundary import BoundaryService
from sose.composition.model import BoundaryMessage
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.composition.resource_intents import IntentResourceCoordinator
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.logistics import simulation as logistics
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
SLOT = timedelta(minutes=5)
INTERVAL = timedelta(hours=1)
POLICY = "composition.deliver_shipment"


def _namespace(prefix: str) -> str:
    return prefix + "_" + uuid4().hex[:12]


def _pool():
    return ResourcePoolContract(
        resource_type="pickup_courier", pool_id="shared-federated-courier",
        scope="shared", capacity=1,
    )


def _init_domain(dsn, ns, org):
    with PostgresPersistence(dsn, namespace=ns) as domain:
        shipment = logistics.seed_reference(domain)
        source = BoundaryMessage.create(
            contract_name="warehouse.dispatch_ready", contract_version=1,
            source_domain="warehouse_fulfillment",
            source_identity=shipment.shipment_id,
            destination_domain="logistics",
            occurrence_key="dispatch-ready", correlation_id=org,
            causation_id=None, produced_at=logistics.ORIGIN,
            payload={"shipment_id": shipment.shipment_id},
        )
        boundary = BoundaryService(domain)
        boundary.publish(source)
        lease = boundary.claim_next(
            owner_id=f"ingress-{org}", now=logistics.ORIGIN,
            lease_duration=timedelta(hours=1),
            destination_domain="logistics",
            accepted_contracts=frozenset({("warehouse.dispatch_ready", 1)}),
        )
        assert lease is not None
        receipt = boundary.consume(
            lease=lease, registry=TradingCustomerRecoveryRunner._registry_for(source),
            now=logistics.ORIGIN,
        )
        return (shipment.shipment_id, source.message_id, receipt.consumer_effect_id)


def _schedule(domain, resources, org):
    return RecoverySchedule(
        TradingCustomerRecoveryRunner(
            persistence=domain, owner_id=f"recovery-{org}",
            job_id=f"recovery-{org}", max_actions=1,
            resource_persistence=resources,
            resource_policies={POLICY: _pool()},
            resource_organizations={org: org},
            resource_slot_duration=SLOT, resource_retry_delay=SLOT,
        ),
        start_at=logistics.ORIGIN, interval=INTERVAL, max_slots=1,
    )


# Deliberately not a graceful exception: closing Python context managers
# cannot be mistaken for an OS-killed worker.
_KILL_AFTER_COMMIT = """
import os
import sys
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.logistics import simulation as logistics
from sose.persistence.postgres import PostgresPersistence
from datetime import timedelta
dsn, domain_ns, resource_ns, org = sys.argv[1:]
original = TradingCustomerRecoveryRunner._execute_pending
def killed(store, source, effect_id, *, authoritative_pickup=None, resource_persistence=None):
    original(store, source, effect_id, authoritative_pickup=authoritative_pickup,
             resource_persistence=resource_persistence)
    assert store.command(effect_id) is None
    assert any(c.effect_id == effect_id for c in store.business_effects())
    os._exit(79)
TradingCustomerRecoveryRunner._execute_pending = staticmethod(killed)
with PostgresPersistence(dsn, namespace=domain_ns) as domain:
    with PostgresPersistence(dsn, namespace=resource_ns) as shared:
        schedule = RecoverySchedule(
            TradingCustomerRecoveryRunner(
                persistence=domain, owner_id="killed-a", job_id=f"recovery-{org}",
                max_actions=1, resource_persistence=shared,
                resource_policies={"composition.deliver_shipment": ResourcePoolContract(
                    resource_type="pickup_courier", pool_id="shared-federated-courier",
                    scope="shared", capacity=1,
                )},
                resource_organizations={org:org}, resource_slot_duration=timedelta(minutes=5),
                resource_retry_delay=timedelta(minutes=5),
            ),
            start_at=logistics.ORIGIN, interval=timedelta(hours=1), max_slots=1,
        )
        schedule.run_due(now=logistics.ORIGIN)
raise AssertionError("worker unexpectedly survived the certified effect")
"""


def _proof(dsn, domain_names, resource_ns, seeds):
    with PostgresPersistence(dsn, namespace=resource_ns) as shared:
        coordinator = IntentResourceCoordinator(
            shared, {POLICY: _pool()}, slot_duration=SLOT, retry_delay=SLOT,
        )
        assert shared.temporal_resources().audit()
        ledger = shared.temporal_resources().snapshot()
        clocks = {
            org: coordinator.logical_time(org)
            for org in sorted(domain_names)
        }
    domains = {}
    for org, ns in sorted(domain_names.items()):
        shipment_id, boundary_id, effect_id = seeds[org]
        with PostgresPersistence(dsn, namespace=ns) as domain:
            shipment = domain.entity("shipment", shipment_id)
            certificate = [c for c in domain.business_effects()
                           if c.effect_id == effect_id]
            assert shipment is not None and shipment.state == "delivered"
            assert len(certificate) == 1
            assert certificate[0].boundary_message_id == boundary_id
            assert certificate[0].correlation_id == org
            assert domain.command(effect_id) is None
            state = domain.job_state(f"recovery-{org}")
            assert state is not None and state.active_trigger_id is None
            domains[org] = {
                "shipment": (shipment.state, shipment.version),
                "events": tuple(
                    (e.event_id, e.causation_id, e.correlation_id)
                    for e in domain.events() if e.entity_type == "shipment"
                ),
                "certificate": tuple(certificate),
                "triggers": tuple(state.completed_batch_triggers),
                "clock": state.logical_time,
                "position": domain.simulation_position(),
            }
    return {"domains": domains, "ledger": ledger, "resource_clocks": clocks}


def _experiment(dsn, *, fault: bool):
    names = {"a": _namespace("pc6orga"), "b": _namespace("pc6orgb")}
    resource_ns = _namespace("pc6pool")
    seeds = {org: _init_domain(dsn, names[org], org) for org in ("a", "b")}
    if fault:
        child = subprocess.run(
            [sys.executable, "-c", _KILL_AFTER_COMMIT, dsn,
             names["a"], resource_ns, "a"],
            capture_output=True, text=True, check=False, timeout=90,
        )
        assert child.returncode == 79, child.stderr
        with PostgresPersistence(dsn, namespace=resource_ns) as shared:
            assert shared.temporal_resources().get(seeds["a"][2]).status == "reserved"
        with PostgresPersistence(dsn, namespace=names["a"]) as dead:
            assert dead.job_state("recovery-a").active_trigger_id is not None
            assert dead.entity("shipment", seeds["a"][0]).state == "delivered"
    else:
        with PostgresPersistence(dsn, namespace=names["a"]) as a:
            with PostgresPersistence(dsn, namespace=resource_ns) as shared:
                assert len(_schedule(a, shared, "a").run_due(now=logistics.ORIGIN)) == 1

    # B progresses its own checkpoint even if A died with a live booking.
    with PostgresPersistence(dsn, namespace=names["b"]) as b:
        with PostgresPersistence(dsn, namespace=resource_ns) as shared:
            schedule = _schedule(b, shared, "b")
            assert len(schedule.run_due(now=logistics.ORIGIN)) == 1
            assert b.job_state("recovery-b").next_tick == 1
            assert shared.temporal_resources().get(seeds["b"][2]) is None
            assert len(schedule.run_due(now=logistics.ORIGIN + INTERVAL)) == 1
            assert b.job_state("recovery-b").next_tick == 2

    if fault:
        # A replays its unfinished ORIGINAL occurrence, reconciles the
        # durable certificate and releases its booking without reapplying
        # the real Logistics transition.
        with PostgresPersistence(dsn, namespace=names["a"]) as a:
            with PostgresPersistence(dsn, namespace=resource_ns) as shared:
                assert len(_schedule(a, shared, "a").run_due(now=logistics.ORIGIN)) == 1
                assert shared.temporal_resources().get(seeds["a"][2]).status == "released"

    with PostgresPersistence(dsn, namespace=names["b"]) as b:
        with PostgresPersistence(dsn, namespace=resource_ns) as shared:
            assert len(_schedule(b, shared, "b").run_due(
                now=logistics.ORIGIN + 2 * INTERVAL,
            )) == 1
            assert shared.temporal_resources().get(seeds["b"][2]).status == "released"
            assert b.job_state("recovery-b").next_tick == 3

    return _proof(dsn, names, resource_ns, seeds)


def test_pg_real_killed_worker_recovers_two_autonomous_organizations_without_global_lock():
    assert DSN
    uninterrupted = _experiment(DSN, fault=False)
    recovered = _experiment(DSN, fault=True)
    assert recovered == uninterrupted
    assert recovered["domains"]["a"]["clock"] == logistics.ORIGIN
    assert recovered["domains"]["b"]["clock"] == logistics.ORIGIN + 2 * INTERVAL
    assert recovered["resource_clocks"]["a"] == logistics.PICKUP_DUE + SLOT
    assert recovered["resource_clocks"]["b"] == logistics.ORIGIN + 2 * INTERVAL + SLOT
    assert len(recovered["ledger"]["reservations"]) == 2
    assert len(recovered["ledger"]["events"]) == 4


def test_pg_independent_org_writer_commits_while_another_org_holds_its_lease():
    """A long-running A transaction must not fence or serialize unrelated B."""
    assert DSN
    a_ns, b_ns, shared_ns = (
        _namespace("pc6writer_a"), _namespace("pc6writer_b"), _namespace("pc6writer_shared")
    )
    completed = Event()
    failure = []

    def run_b():
        try:
            with PostgresPersistence(DSN, namespace=b_ns) as b:
                with PostgresPersistence(DSN, namespace=shared_ns) as shared:
                    slot = _schedule(b, shared, "b")
                    assert len(slot.run_due(now=logistics.ORIGIN)) == 1
                    assert b.job_state("recovery-b").next_tick == 1
        except BaseException as exc:
            failure.append(exc)
        finally:
            completed.set()

    with PostgresPersistence(DSN, namespace=a_ns) as a:
        owned = a.claim_writer("organization-a", expected_epoch=a.writer_epoch())
        worker = Thread(target=run_b, daemon=True)
        # Deliberately keep A's writer transaction and namespace-scoped
        # advisory lock open while B completes on its own connection.
        with a.transaction(owner_epoch=owned.epoch):
            worker.start()
            assert completed.wait(20), "B was blocked by unrelated organizational writer"
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert not failure, failure
        assert a.writer_epoch() == owned.epoch

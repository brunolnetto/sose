"""PC6: independently fenced recurring jobs and organizational restart clocks.

A global namespace writer epoch must not invalidate another organization's
in-flight job when the two operate on disjoint state and share only genuinely
finite physical resource pools.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import os

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.postgres import PostgresPersistence, StaleWriterError

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _namespace():
    return "pc6_recur_" + uuid4().hex[:12]


def test_scoped_pg_epochs_fence_only_the_same_job():
    assert DSN
    ns = _namespace()
    with (
        PostgresPersistence(DSN, namespace=ns) as first,
        PostgresPersistence(DSN, namespace=ns) as second,
        PostgresPersistence(DSN, namespace=ns) as successor,
    ):
        a = first.claim_scoped_writer(
            "pc6-job-organization-a", "worker-a",
            expected_epoch=first.scoped_writer_epoch("pc6-job-organization-a"),
        )
        b = second.claim_scoped_writer(
            "pc6-job-organization-b", "worker-b",
            expected_epoch=second.scoped_writer_epoch("pc6-job-organization-b"),
        )
        with FencedEnginePersistence(first, a).transaction() as uow:
            assert uow.get_job_state("pc6-job-organization-a") is None
        with FencedEnginePersistence(second, b).transaction() as uow:
            assert uow.get_job_state("pc6-job-organization-b") is None
        successor.claim_scoped_writer(
            "pc6-job-organization-a", "worker-a-recovered",
            expected_epoch=a.epoch,
        )
        with pytest.raises(StaleWriterError, match="stale scoped writer"):
            with FencedEnginePersistence(first, a).transaction():
                pass
        # The unrelated organization was not fenced out by recovery of A.
        with FencedEnginePersistence(second, b).transaction():
            pass


def test_crash_during_recurrence_replays_only_its_job_slot(monkeypatch):
    assert DSN
    ns = _namespace()
    slots = {"organization-a": T0, "organization-b": T0 + timedelta(hours=2)}
    observations = []
    failure = {"armed": True}

    def bounded(self, store, *, now, logical_now=None):
        scope = store.lease.scope
        # Physical lease/observation time may be later on restart; the
        # persisted schedule's logical time must remain the original slot.
        observations.append((scope, now, store.job_state(self.job_id).logical_time))
        if scope == "job-a" and failure["armed"]:
            failure["armed"] = False
            raise RuntimeError("simulated worker death after durable checkpoint")
        return 1

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", bounded)

    def job(store, org, job_id, owner):
        return RecoverySchedule(
            runner=TradingCustomerRecoveryRunner(
                persistence=store, owner_id=owner, job_id=job_id,
                correlation_id=org, scoped_writer=True,
            ),
            start_at=slots[org], interval=timedelta(minutes=5),
            max_slots=2,
        )

    with PostgresPersistence(DSN, namespace=ns) as failed_a:
        with pytest.raises(RuntimeError, match="simulated worker death"):
            job(failed_a, "organization-a", "job-a", "worker-a").run_due(now=T0)
        checkpoint = failed_a.job_state("job-a")
        assert checkpoint is not None
        assert checkpoint.active_trigger_id is not None
        assert checkpoint.logical_time == T0

    # B is allowed to advance its own time even while A's slot remains active.
    with PostgresPersistence(DSN, namespace=ns) as healthy_b:
        results = job(healthy_b, "organization-b", "job-b", "worker-b").run_due(
            now=slots["organization-b"],
        )
        assert len(results) == 1 and results[0].actions == 1
        assert healthy_b.job_state("job-b").logical_time == slots["organization-b"]
        assert healthy_b.job_state("job-a").active_trigger_id is not None

    with PostgresPersistence(DSN, namespace=ns) as recovered_a:
        results = job(recovered_a, "organization-a", "job-a", "worker-a-recovered").run_due(
            now=T0 + timedelta(minutes=5),
        )
        assert len(results) == 2
        assert recovered_a.job_state("job-a").logical_time == T0 + timedelta(minutes=5)
        assert recovered_a.job_state("job-b").logical_time == slots["organization-b"]
        assert recovered_a.job_state("job-a").active_trigger_id is None
        assert recovered_a.job_state("job-b").active_trigger_id is None
        assert recovered_a.job_state("job-a").run_count == 2
        assert recovered_a.job_state("job-b").run_count == 1
        assert observations == [
            ("job-a", T0, T0),
            ("job-b", slots["organization-b"], slots["organization-b"]),
            ("job-a", T0 + timedelta(minutes=5), T0),
            ("job-a", T0 + timedelta(minutes=5), T0 + timedelta(minutes=5)),
        ]


def test_two_distinct_job_scopes_can_be_in_flight_together(monkeypatch):
    assert DSN
    ns = _namespace()
    barrier = Barrier(2)

    def bounded(self, store, *, now, logical_now=None):
        barrier.wait(timeout=12)
        with store.transaction() as uow:
            own = uow.get_job_state(self.job_id)
            other = uow.get_job_state("job-b" if self.job_id == "job-a" else "job-a")
            assert own is not None and own.active_trigger_id is not None
            assert other is not None and other.active_trigger_id is not None
        return 0

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", bounded)

    def worker(key):
        with PostgresPersistence(DSN, namespace=ns) as store:
            runner = TradingCustomerRecoveryRunner(
                persistence=store, owner_id=f"worker-{key}",
                job_id=f"job-{key}", scoped_writer=True, correlation_id=f"org-{key}",
            )
            return runner.run_scheduled_trigger(
                scheduled_for=T0 + timedelta(minutes=5 if key == "b" else 0),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, key) for key in ("a", "b")]
        results = [future.result(timeout=25) for future in futures]
    assert {result.job_id for result in results} == {"job-a", "job-b"}
    with PostgresPersistence(DSN, namespace=ns) as recovered:
        assert recovered.job_state("job-a").run_count == 1
        assert recovered.job_state("job-b").run_count == 1


def test_recurring_jobs_claim_only_their_own_causal_boundary(monkeypatch):
    """Real PostgreSQL ACKs, not synthetic work callbacks, stay org scoped."""
    from sose.composition.boundary import BoundaryService
    from sose.composition.model import BoundaryMessage, DeliveryStatus

    assert DSN
    ns = _namespace()
    messages = {}
    with PostgresPersistence(DSN, namespace=ns) as seed:
        for org in ("organization-a", "organization-b"):
            message = BoundaryMessage.create(
                contract_name="warehouse.dispatch_ready", contract_version=1,
                source_domain="warehouse_fulfillment",
                source_identity=f"fulfillment-{org}",
                destination_domain="logistics", occurrence_key="dispatch-ready",
                correlation_id=org, causation_id=None, produced_at=T0,
                payload={"shipment_id": f"shipment-{org}"},
            )
            BoundaryService(seed).publish(message)
            messages[org] = message

    def run(org, job_id, logical_time):
        with PostgresPersistence(DSN, namespace=ns) as store:
            runner = TradingCustomerRecoveryRunner(
                persistence=store, owner_id=f"worker-{org}", job_id=job_id,
                correlation_id=org, scoped_writer=True, max_actions=1,
            )
            completed = runner.run_scheduled_trigger(scheduled_for=logical_time)
            assert completed.actions == 1
            with store.transaction() as uow:
                statuses = tuple(
                    (d.message_id, d.status) for d in uow.boundary_deliveries()
                )
            return store.job_state(job_id).logical_time, statuses

    time_a, deliveries = run("organization-a", "job-a", T0)
    assert time_a == T0
    by_message = dict(deliveries)
    assert by_message[messages["organization-a"].message_id] is DeliveryStatus.CONSUMED
    assert by_message[messages["organization-b"].message_id] is DeliveryStatus.PENDING

    time_b, deliveries = run("organization-b", "job-b", T0 + timedelta(hours=2))
    assert time_b == T0 + timedelta(hours=2)
    assert all(status is DeliveryStatus.CONSUMED for _, status in deliveries)
    with PostgresPersistence(DSN, namespace=ns) as restored:
        assert restored.job_state("job-a").logical_time == T0
        assert restored.job_state("job-b").logical_time == T0 + timedelta(hours=2)
        with restored.transaction() as uow:
            consumptions = [
                uow.get_boundary_consumption(d.delivery_id)
                for d in uow.boundary_deliveries()
            ]
            assert len(consumptions) == 2
            assert all(receipt is not None for receipt in consumptions)
            assert all(uow.get_command(receipt.consumer_effect_id) is not None
                       for receipt in consumptions)


def test_real_logistics_certified_crash_keeps_other_organization_progressing(monkeypatch):
    """Real Logistics statecharts and PG physical bookings: cert-before-release crash."""
    from sose.composition.boundary import BoundaryService
    from sose.composition.model import BoundaryMessage
    from sose.composition.resource_intents import IntentResourceCoordinator
    from sose.core.resource_identity import ResourcePoolContract
    from sose.examples.logistics import simulation as logistics

    assert DSN
    ns = _namespace()
    orgs = ("organization-a", "organization-b")
    pool = ResourcePoolContract(
        resource_type="pickup_courier", pool_id="pc6-shared-dispatch",
        scope="shared", capacity=2,
    )
    effects = {}
    shipments = {}
    with PostgresPersistence(DSN, namespace=ns) as original:
        service = BoundaryService(original)
        for org in orgs:
            entities = logistics.seed_reference(original, instance_key=org)
            shipments[org] = entities.shipment_id
            fulfillment_id = f"fulfillment-{org}"
            upstream = BoundaryMessage.create(
                contract_name="o2c.fulfillment_requested", contract_version=2,
                source_domain="order_to_cash", source_identity=f"order-{org}",
                destination_domain="warehouse_fulfillment",
                occurrence_key="fulfillment-requested", correlation_id=org,
                causation_id=None, produced_at=logistics.ORIGIN,
                payload={
                    "order_id": f"order-{org}",
                    "fulfillment_order_id": fulfillment_id,
                    "shipment_id": entities.shipment_id,
                },
            )
            service.publish(upstream)
            source = BoundaryMessage.create(
                contract_name="warehouse.dispatch_ready", contract_version=1,
                source_domain="warehouse_fulfillment",
                source_identity=entities.shipment_id,
                destination_domain="logistics", occurrence_key="dispatch-ready",
                correlation_id=org, causation_id=None,
                produced_at=logistics.ORIGIN,
                payload={
                    "shipment_id": entities.shipment_id,
                    "fulfillment_order_id": fulfillment_id,
                },
            )
            service.publish(source)
            boundary_lease = service.claim_next(
                owner_id="ingress-worker", now=logistics.ORIGIN,
                lease_duration=timedelta(hours=1),
                destination_domain="logistics", message_id=source.message_id,
            )
            assert boundary_lease is not None
            ack = service.consume(
                lease=boundary_lease,
                registry=TradingCustomerRecoveryRunner._registry_for(source),
                now=logistics.ORIGIN,
            )
            effects[org] = ack.consumer_effect_id

    def schedule(store, org, owner):
        return RecoverySchedule(
            runner=TradingCustomerRecoveryRunner(
                persistence=store, owner_id=owner, job_id="job-" + org,
                correlation_id=org, scoped_writer=True, max_actions=1,
                resource_policies={"composition.deliver_shipment": pool},
                resource_organizations={org: org},
                resource_slot_duration=timedelta(minutes=5),
                resource_retry_delay=timedelta(minutes=5),
            ),
            start_at=logistics.ORIGIN + (
                timedelta(minutes=5) if org == "organization-b" else timedelta()
            ),
            interval=timedelta(minutes=5), max_slots=1,
        )

    original_complete = IntentResourceCoordinator.complete
    armed = {"crash": True}

    def die_after_domain_certificate(self, effect_id):
        if effect_id == effects["organization-a"] and armed["crash"]:
            armed["crash"] = False
            with self._store.transaction(
                owner_epoch=self._owner_epoch, writer_scope=self._writer_scope,
            ) as uow:
                proof = uow.get_business_effect(effect_id)
                assert proof is not None and proof.terminal_state == "delivered"
                assert uow.get_command(effect_id) is None
            assert self._ledger.get(effect_id).status == "reserved"
            raise RuntimeError("killed after real Logistics certification")
        return original_complete(self, effect_id)

    monkeypatch.setattr(IntentResourceCoordinator, "complete", die_after_domain_certificate)
    with PostgresPersistence(DSN, namespace=ns) as first_a:
        with pytest.raises(RuntimeError, match="killed after real Logistics certification"):
            schedule(first_a, orgs[0], "a-first").run_due(now=logistics.ORIGIN)
        assert first_a.job_state("job-organization-a").active_trigger_id is not None
        assert first_a.entity("shipment", shipments[orgs[0]]).state == "delivered"
        assert first_a.temporal_resources().get(effects[orgs[0]]).status == "reserved"

    with PostgresPersistence(DSN, namespace=ns) as b:
        b_slots = schedule(b, orgs[1], "b-healthy").run_due(
            now=logistics.ORIGIN + timedelta(minutes=5),
        )
        assert len(b_slots) == 1
        assert b.entity("shipment", shipments[orgs[1]]).state == "delivered"
        assert b.temporal_resources().get(effects[orgs[1]]).status == "released"
        # A's certified effect and unreleased booking are not B's to reconcile.
        assert b.temporal_resources().get(effects[orgs[0]]).status == "reserved"
        assert b.job_state("job-organization-b").run_count == 1
        assert b.job_state("job-organization-a").run_count == 0

    with PostgresPersistence(DSN, namespace=ns) as recovered_a:
        recovered_slots = schedule(recovered_a, orgs[0], "a-recovered").run_due(
            now=logistics.ORIGIN + timedelta(minutes=5),
        )
        assert len(recovered_slots) == 1
        ledger = recovered_a.temporal_resources()
        assert ledger.audit()
        assert all(ledger.get(effects[org]).status == "released" for org in orgs)
        assert all(
            recovered_a.entity("shipment", shipments[org]).state == "delivered"
            for org in orgs
        )
        assert all(
            sum(e.effect_id == effects[org] for e in recovered_a.business_effects()) == 1
            for org in orgs
        )
        assert recovered_a.job_state("job-organization-a").run_count == 1
        assert recovered_a.job_state("job-organization-b").run_count == 1
        assert recovered_a.job_state("job-organization-a").logical_time == logistics.ORIGIN
        assert recovered_a.job_state("job-organization-b").logical_time == (
            logistics.ORIGIN + timedelta(minutes=5)
        )
        assert len({ledger.get(effects[org]).owner_id for org in orgs}) == 2
        assert not any(
            r.resource_name == "pickup_courier"
            for r in recovered_a.resource_reservations()
        )


def test_scoped_job_cannot_silently_rebind_another_organization():
    assert DSN
    ns = _namespace()
    with PostgresPersistence(DSN, namespace=ns) as store:
        a = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker-a", job_id="shared-job",
            scoped_writer=True, correlation_id="organization-a", max_actions=1,
        )
        a.run_scheduled_trigger(scheduled_for=T0)
        original = store.job_state("shared-job")
        b = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker-b", job_id="shared-job",
            scoped_writer=True, correlation_id="organization-b", max_actions=1,
        )
        with pytest.raises(ValueError, match="organizational binding changed"):
            b.run_scheduled_trigger(scheduled_for=T0 + timedelta(minutes=5))
        assert store.job_state("shared-job") == original


def test_correlation_cannot_be_claimed_by_two_distinct_recurring_jobs():
    assert DSN
    ns = _namespace()
    with (
        PostgresPersistence(DSN, namespace=ns) as a,
        PostgresPersistence(DSN, namespace=ns) as b,
    ):
        first = TradingCustomerRecoveryRunner(
            persistence=a, owner_id="worker-a", job_id="company-a-job",
            scoped_writer=True, correlation_id="shared-correlation",
            max_actions=1,
        )
        first.run_scheduled_trigger(scheduled_for=T0)
        different = TradingCustomerRecoveryRunner(
            persistence=b, owner_id="worker-b", job_id="other-job",
            scoped_writer=True, correlation_id="shared-correlation",
            max_actions=1,
        )
        with pytest.raises(ValueError, match="already owned by another job"):
            different.run_scheduled_trigger(scheduled_for=T0)
        assert b.job_state("other-job") is None
        assert b.job_state("company-a-job").run_count == 1


class _KilledAfterCheckpoint(TradingCustomerRecoveryRunner):
    """This test worker receives an actual SIGKILL outside an open transaction."""

    def _run_bounded(self, store, *, now, logical_now=None):
        import signal
        state = store.job_state(self.job_id)
        assert state is not None and state.active_trigger_id is not None
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("SIGKILL returned unexpectedly")


def _child_die_during_scoped_trigger(namespace):
    with PostgresPersistence(DSN, namespace=namespace) as store:
        runner = _KilledAfterCheckpoint(
            persistence=store, owner_id="killed-process",
            job_id="job-a", scoped_writer=True, correlation_id="org-a",
            max_actions=1,
        )
        runner.run_scheduled_trigger(scheduled_for=T0)


def test_actual_sigkill_restarts_a_job_while_another_advances():
    import multiprocessing
    import signal

    assert DSN
    ns = _namespace()
    child = multiprocessing.get_context("spawn").Process(
        target=_child_die_during_scoped_trigger, args=(ns,),
    )
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.kill()
        child.join(timeout=10)
        pytest.fail("scoped recovery child did not reach its durable kill window")
    assert child.exitcode == -signal.SIGKILL

    with PostgresPersistence(DSN, namespace=ns) as other:
        dead_checkpoint = other.job_state("job-a")
        assert dead_checkpoint is not None and dead_checkpoint.active_trigger_id is not None
        other_runner = TradingCustomerRecoveryRunner(
            persistence=other, owner_id="unaffected-worker",
            job_id="job-b", scoped_writer=True,
            correlation_id="org-b", max_actions=1,
        )
        other_runner.run_scheduled_trigger(scheduled_for=T0 + timedelta(hours=2))
        assert other.job_state("job-b").run_count == 1
        assert other.job_state("job-a") == dead_checkpoint

    with PostgresPersistence(DSN, namespace=ns) as restored:
        runner = TradingCustomerRecoveryRunner(
            persistence=restored, owner_id="replacement-worker",
            job_id="job-a", scoped_writer=True,
            correlation_id="org-a", max_actions=1,
        )
        slot = runner.run_scheduled_trigger(scheduled_for=T0)
        assert slot.actions == 0
        assert restored.job_state("job-a").active_trigger_id is None
        assert restored.job_state("job-a").logical_time == T0
        assert restored.job_state("job-b").logical_time == T0 + timedelta(hours=2)


def test_scoped_boundary_acks_do_not_require_a_global_causal_mutex():
    assert DSN
    ns = _namespace()
    barrier = Barrier(2)

    def scoped_boundary(job, correlation):
        with PostgresPersistence(DSN, namespace=ns) as store:
            lease = store.claim_scoped_writer(
                job, "worker-" + job,
                expected_epoch=store.scoped_writer_epoch(job),
                correlation_id=correlation,
            )
            fenced = FencedEnginePersistence(store, lease)
            with fenced.boundary_transaction() as uow:
                assert uow.get_job_state(job) is None
                # Both transactions must be in their causal critical sections
                # concurrently; a namespace-global exclusive lock deadlocks.
                barrier.wait(timeout=12)
            return job

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(scoped_boundary, "job-a", "organization-a"),
            pool.submit(scoped_boundary, "job-b", "organization-b"),
        ]
        assert {f.result(timeout=25) for f in futures} == {"job-a", "job-b"}

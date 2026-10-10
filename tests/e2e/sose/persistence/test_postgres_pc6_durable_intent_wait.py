"""PC6: durable resource wait and distinct organization clocks under restart."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os

import pytest

from sose.composition.model import BoundaryMessage
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.resource_intents import IntentResourceCoordinator
from sose.core.events import Command
from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import ResourceConflictError
from sose.persistence.postgres import PostgresPersistence, StaleWriterError

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
SLOT = timedelta(minutes=5)
POLICY_NAME = "composition.deliver_shipment"


def _namespace(prefix):
    return prefix + "_" + uuid4().hex[:12]


def _pool():
    return ResourcePoolContract(
        resource_type="pickup_courier",
        pool_id="pc6-shared-dispatch",
        scope="shared", capacity=1,
    )


def _coordinator(store):
    return IntentResourceCoordinator(
        store, {POLICY_NAME: _pool()},
        slot_duration=SLOT, retry_delay=SLOT,
    )


def _admit(coordinator, effect_id, org, time):
    return coordinator.admit(
        effect_id=effect_id, intent_name=POLICY_NAME,
        organization_id=org, due_at=T0, now=time,
        causation_id=f"boundary:{org}",
    )


def test_pg_intent_capacity_wait_survives_restart_without_double_booking():
    assert DSN
    ns = _namespace("pc6wait")
    with PostgresPersistence(DSN, namespace=ns) as first:
        coordinator = _coordinator(first)
        assert _admit(coordinator, "effect-a", "organization-a", T0) is True
        assert _admit(coordinator, "effect-b", "organization-b", T0) is False
        wait = coordinator.waiting("effect-b")
        assert wait is not None and wait.ready_at == T0+SLOT and wait.attempts == 1
        assert coordinator.logical_time("organization-a") == T0
        assert coordinator.logical_time("organization-b") is None
        coordinator.complete("effect-a")

    with PostgresPersistence(DSN, namespace=ns) as restored:
        coordinator = _coordinator(restored)
        assert not _admit(coordinator, "effect-b", "organization-b", T0+SLOT/2)
        assert coordinator.waiting("effect-b").attempts == 1
        assert _admit(coordinator, "effect-b", "organization-b", T0+SLOT)
        assert coordinator.waiting("effect-b") is None
        coordinator.complete("effect-b")
        assert coordinator.logical_time("organization-a") == T0+SLOT
        assert coordinator.logical_time("organization-b") == T0+2*SLOT
        assert coordinator._ledger.audit()
        before = coordinator._ledger.snapshot()

    with PostgresPersistence(DSN, namespace=ns) as again:
        coordinator = _coordinator(again)
        assert _admit(coordinator, "effect-b", "organization-b", T0+3*SLOT)
        coordinator.complete("effect-b")
        assert coordinator._ledger.snapshot() == before
        assert len(coordinator._ledger.reservations()) == 2
        assert len(coordinator._ledger.events()) == 4
        with pytest.raises(ResourceConflictError, match="ownership"):
            _admit(coordinator, "effect-b", "wrong-org", T0+4*SLOT)


def _source(key):
    return BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity=key, destination_domain="logistics",
        occurrence_key="dispatch-ready",
        correlation_id=key, causation_id=None,
        produced_at=T0, payload={"shipment_id": f"shipment-{key}"},
    )


def _execute(namespace, monkeypatch, *, restart):
    sources = {key: _source(key) for key in ("organization-a", "organization-b")}
    effects = {
        key: Command(
            command_id=f"effect-{key}", name=POLICY_NAME,
            entity_type="shipment", entity_id=f"shipment-{key}",
            due_at=T0, issued_at=T0,
            causation_id=sources[key].message_id,
            correlation_id=key,
        )
        for key in sources
    }
    executed = []

    def pending(store):
        return tuple(
            (sources[key], effects[key].command_id)
            for key in sorted(sources)
            if store.command(effects[key].command_id) is not None
        )

    def execute(store, source, effect_id):
        executed.append((source.correlation_id, effect_id))
        with store.transaction() as uow:
            uow.delete_command(effect_id)

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_pending_effects", staticmethod(pending))
    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_execute_pending", staticmethod(execute))

    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction() as uow:
            for effect in effects.values():
                uow.save_command(effect)
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="resource-worker",
            resource_policies={POLICY_NAME: _pool()},
            resource_organizations={"organization-a":"organization-a", "organization-b":"organization-b"},
            resource_slot_duration=SLOT, resource_retry_delay=SLOT,
        )
        assert runner._run_bounded(store, now=T0, logical_now=T0) == 1
        assert store.command(effects["organization-b"].command_id) is not None
        assert _coordinator(store).waiting(effects["organization-b"].command_id)

        if not restart:
            assert runner._run_bounded(
                store, now=T0+SLOT, logical_now=T0+SLOT,
            ) == 1
    if restart:
        with PostgresPersistence(DSN, namespace=namespace) as reopened:
            runner = TradingCustomerRecoveryRunner(
                persistence=reopened, owner_id="recovered-worker",
                resource_policies={POLICY_NAME: _pool()},
            resource_organizations={"organization-a":"organization-a", "organization-b":"organization-b"},
                resource_slot_duration=SLOT, resource_retry_delay=SLOT,
            )
            assert runner._run_bounded(
                reopened, now=T0+SLOT, logical_now=T0+SLOT,
            ) == 1

    with PostgresPersistence(DSN, namespace=namespace) as final:
        coordinator = _coordinator(final)
        assert coordinator.waiting(effects["organization-b"].command_id) is None
        assert all(final.command(cmd.command_id) is None for cmd in effects.values())
        return (
            coordinator._ledger.snapshot(),
            coordinator.logical_time("organization-a"),
            coordinator.logical_time("organization-b"),
            tuple(executed),
        )


def test_pg_recovery_runner_defers_contended_intent_without_starvation_or_rewind(monkeypatch):
    assert DSN
    baseline = _execute(_namespace("pc6base"), monkeypatch, restart=False)
    recovered = _execute(_namespace("pc6fault"), monkeypatch, restart=True)
    assert recovered == baseline
    assert [x[0] for x in recovered[3]] == ["organization-a", "organization-b"]
    assert recovered[1] == T0+SLOT
    assert recovered[2] == T0+2*SLOT


def test_pg_unmapped_domain_intent_never_consumes_an_unrelated_pool():
    assert DSN
    with PostgresPersistence(DSN, namespace=_namespace("pc6unmapped")) as store:
        coordinator = _coordinator(store)
        assert coordinator.admit(
            effect_id="unmapped", intent_name="composition.complete_external_fulfillment",
            organization_id="organization-a", due_at=T0, now=T0,
        )
        assert coordinator._ledger.reservations() == ()
        assert coordinator.logical_time("organization-a") is None


def test_pg_superseded_worker_epoch_cannot_book_or_advance_another_organization():
    assert DSN
    ns = _namespace("pc6fencing")
    with (
        PostgresPersistence(DSN, namespace=ns) as first,
        PostgresPersistence(DSN, namespace=ns) as successor,
    ):
        lease = first.claim_writer("owner-1", expected_epoch=first.writer_epoch())
        guarded = IntentResourceCoordinator(
            first, {POLICY_NAME: _pool()},
            slot_duration=SLOT, retry_delay=SLOT,
            owner_epoch=lease.epoch,
        )
        assert _admit(guarded, "effect-a", "organization-a", T0)
        successor.claim_writer("owner-2", expected_epoch=lease.epoch)
        with pytest.raises(StaleWriterError, match="stale writer epoch"):
            _admit(guarded, "effect-b", "organization-b", T0+SLOT)
        assert guarded.waiting("effect-b") is None
        assert guarded.logical_time("organization-b") is None
        assert {r.reservation_id for r in successor.temporal_resources().reservations()} == {
            "effect-a"
        }


def test_pg_two_correlations_of_one_organization_share_monotonic_resource_time():
    assert DSN
    with PostgresPersistence(DSN, namespace=_namespace("pc6orgclock")) as store:
        coordinator = _coordinator(store)
        assert coordinator.admit(
            effect_id="order-one", intent_name=POLICY_NAME,
            organization_id="same-company", due_at=T0, now=T0,
            causation_id="message-one",
        )
        coordinator.complete("order-one")
        assert coordinator.logical_time("same-company") == T0+SLOT
        assert coordinator.admit(
            effect_id="order-two", intent_name=POLICY_NAME,
            organization_id="same-company", due_at=T0, now=T0,
            causation_id="message-two",
        )
        second = coordinator._ledger.get("order-two")
        assert second.start_at == T0+SLOT
        assert second.causation_id == "message-two"
        coordinator.complete("order-two")
        assert coordinator.logical_time("same-company") == T0+2*SLOT
        assert coordinator.logical_time("unrelated-company") is None
        assert coordinator._ledger.audit()

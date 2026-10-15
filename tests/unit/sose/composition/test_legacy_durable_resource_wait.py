"""A durable transient resource demand is never a terminal business failure."""
from datetime import datetime, timezone

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.model import BoundaryMessage
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.events import Command
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
TRANSFER_OWNER = "pc6-transfer-recovery-organization"


def test_recovery_defers_a_transient_physical_wait_to_the_next_recurring_tick(
    tmp_path, monkeypatch,
):
    source = BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse_fulfillment", source_identity="order",
        destination_domain="logistics", occurrence_key="pickup",
        correlation_id="flow-a", causation_id=None, produced_at=NOW,
        payload={"shipment_id": "shipment-a"},
    )
    cmd = Command(
        command_id="pending-delivery", name="composition.deliver_shipment",
        entity_type="shipment", entity_id="shipment-a", due_at=NOW, issued_at=NOW,
        causation_id=source.message_id, correlation_id=source.correlation_id,
        owner_id="organization-a",
    )
    attempts = []

    def pending(store):
        return ((source, cmd.command_id),) if store.command(cmd.command_id) else ()

    def process(store, source, effect_id, *, organization_id):
        assert organization_id == "organization-a"
        attempts.append(effect_id)
        if len(attempts) == 1:
            raise customer.DurableResourceWait("pickup_courier", "pickup-courier:shipment-a")
        with store.transaction() as uow:
            uow.delete_command(effect_id)

    monkeypatch.setattr(
        TradingCustomerRecoveryRunner, "_pending_effects", staticmethod(pending),
    )
    monkeypatch.setattr(
        TradingCustomerRecoveryRunner, "_execute_pending", staticmethod(process),
    )

    database = tmp_path / "durable-pickup.sqlite"
    with SQLiteIncrementalPersistence(database) as store:
        with store.transaction() as uow:
            uow.save_command(cmd)
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker-before-death", max_actions=3,
            organization_owners={"flow-a": "organization-a"},
        )
        first = runner.run_scheduled_trigger(scheduled_for=NOW)
        assert first.actions == 0
        assert store.command(cmd.command_id) == cmd
        assert attempts == [cmd.command_id]
    # A different worker and a new persistence object replay the original
    # durable Command on a different recurring slot.
    with SQLiteIncrementalPersistence(database) as restored:
        runner = TradingCustomerRecoveryRunner(
            persistence=restored, owner_id="worker-after-restart", max_actions=3,
            organization_owners={"flow-a": "organization-a"},
        )
        second = runner.run_scheduled_trigger(scheduled_for=NOW.replace(minute=1))
        assert second.actions == 1
        assert attempts == [cmd.command_id, cmd.command_id]
        assert restored.command(cmd.command_id) is None
        assert runner.run_scheduled_trigger(
            scheduled_for=NOW.replace(minute=2),
        ).actions == 0


def test_synchronous_reference_path_retries_only_typed_temporary_capacity_wait(
    monkeypatch,
):
    original = customer._execute_intent
    attempts = []

    def transient_once(store, *, effect_id, fixtures, correlation_id, organization_id):
        effect = store.command(effect_id)
        if effect is not None and effect.name == "composition.deliver_shipment":
            attempts.append(effect_id)
            if len(attempts) == 1:
                raise customer.DurableResourceWait(
                    "pickup_courier", f"pickup-courier:{effect.entity_id}",
                )
        return original(
            store, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id, organization_id=organization_id,
        )

    monkeypatch.setattr(customer, "_execute_intent", transient_once)
    result = customer.run_customer_demand_path(organization_id="test-org-legacy-durable-resource-wait")
    assert len(attempts) == 2
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"


def test_real_logistics_origin_dock_contention_is_durable_wait(monkeypatch):
    """A second independent workflow cannot treat a finite dock queue as failure."""
    original = customer.logistics.reconcile_origin_hub
    calls = []
    blocker = "origin-dock:concurrent-blocker"

    def contend_once(persistence, engine, backend, *, entities):
        calls.append(entities.shipment_id)
        if len(calls) == 1:
            holding = engine.resources.ensure_requested(
                backend, resource_name="origin_dock",
                request_id=blocker, requested_at=backend.now,
            )
            backend.run_until(backend.now)
            assert holding is not None
        else:
            engine.resources.withdraw(backend, blocker)
            backend.run_until(backend.now)
        return original(
            persistence, engine, backend, entities=entities,
        )

    monkeypatch.setattr(customer.logistics, "reconcile_origin_hub", contend_once)
    result = customer.run_customer_demand_path(organization_id="test-org-legacy-durable-resource-wait")
    assert len(calls) >= 2
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"
    assert result.persistence.business_effects()


@pytest.mark.parametrize(
    ("resource_name", "blocker_id"),
    [
        ("transfer_vehicle", "transfer-vehicle:external-blocker"),
        ("destination_dock", "destination-dock:external-blocker"),
    ],
)
def test_composed_logistics_transfer_resource_contention_is_a_durable_wait(
    monkeypatch, resource_name, blocker_id,
):
    """A finite transfer leg must retry its original Command, not fail business."""
    original = customer.logistics.reconcile_transfer
    calls = []

    def contend_once(persistence, engine, backend, *, entities):
        calls.append(entities.shipment_id)
        if len(calls) == 1:
            holding = engine.resources.ensure_requested(
                backend, resource_name=resource_name,
                request_id=blocker_id, requested_at=backend.now,
            )
            backend.run_until(backend.now)
            assert holding is not None
        else:
            engine.resources.withdraw(backend, blocker_id)
            backend.run_until(backend.now)
        return original(
            persistence, engine, backend, entities=entities,
        )

    monkeypatch.setattr(customer.logistics, "reconcile_transfer", contend_once)
    result = customer.run_customer_demand_path(organization_id=TRANSFER_OWNER)
    assert len(calls) >= 2
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"
    certified = result.persistence.business_effects()
    assert certified
    assert len(certified) == len({effect.effect_id for effect in certified})


def test_unsubstantiated_transfer_failure_remains_terminal(monkeypatch):
    """No pending request, reservation, queue GET or PUT means no made-up wait."""
    def fails_without_durable_demand(_persistence, _engine, _backend, *, entities):
        return False

    monkeypatch.setattr(
        customer.logistics, "reconcile_transfer", fails_without_durable_demand,
    )
    with pytest.raises(RuntimeError, match="logistics transfer failed"):
        customer.run_customer_demand_path(organization_id=TRANSFER_OWNER)



def test_origin_dequeue_committed_between_reconcile_and_classification_is_retryable(
    monkeypatch,
):
    """A raced-off pending GET is not a terminal error if exact receipt exists.

    This forces the interleaving: one worker reports a stale False just after
    a different worker has committed the same durable origin dequeue. No
    pending StoreGetRequest remains, but the StoreGetResult is authoritative.
    """
    original = customer.logistics._ensure_origin_queue_dequeue
    observed = []

    def committed_just_after_read(persistence, engine, backend, *, shipment_id):
        reconciled = original(
            persistence, engine, backend, shipment_id=shipment_id,
        )
        if not observed and reconciled:
            receipt = next(
                (
                    result for result in persistence.store_get_results()
                    if result.request_id == f"origin-dequeue:{shipment_id}"
                    and result.item.item_id == f"origin-queue:{shipment_id}"
                    and result.store_name == f"{TRANSFER_OWNER}/origin_hub_queue"
                ),
                None,
            )
            assert receipt is not None
            assert not any(
                req.request_id == f"origin-dequeue:{shipment_id}"
                for req in persistence.store_get_requests()
            )
            observed.append(receipt.request_id)
            return False
        return reconciled

    monkeypatch.setattr(
        customer.logistics, "_ensure_origin_queue_dequeue",
        committed_just_after_read,
    )
    result = customer.run_customer_demand_path(organization_id=TRANSFER_OWNER)
    assert len(observed) == 1
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"
    results = (
        x for x in result.persistence.store_get_results()
        if x.request_id == observed[0]
    )
    assert len(tuple(results)) == 1
    effects = result.persistence.business_effects()
    assert len(effects) == len({effect.effect_id for effect in effects})



def test_transfer_grant_committed_after_stale_false_remains_retryable(monkeypatch):
    """A granted finite vehicle is progress, not evidence the demand disappeared."""
    original = customer.logistics.reconcile_transfer
    attempts = []

    def granted_after_reconcile_read(persistence, engine, backend, *, entities):
        attempts.append(entities.shipment_id)
        if len(attempts) == 1:
            request_id = f"transfer-vehicle:{entities.shipment_id}"
            granted = engine.resources.ensure_requested(
                backend, resource_name="transfer_vehicle",
                request_id=request_id, requested_at=backend.now,
            )
            assert granted is not None
            assert engine.resources.reservation_for(request_id) is not None
            assert not any(
                demand.request_id == request_id
                for demand in persistence.resource_demands()
            )
            # The earlier false result was from before this durable grant.
            return False
        return original(persistence, engine, backend, entities=entities)

    monkeypatch.setattr(customer.logistics, "reconcile_transfer", granted_after_reconcile_read)
    result = customer.run_customer_demand_path(organization_id=TRANSFER_OWNER)
    assert len(attempts) >= 2
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"
    effects = result.persistence.business_effects()
    assert len(effects) == len({effect.effect_id for effect in effects})


def test_destination_put_committed_after_false_and_intent_removed_is_retryable(monkeypatch):
    """A concurrent accepted destination put must never become terminal failure."""
    original = customer.logistics.reconcile_transfer
    attempts = []

    def complete_pending_put_after_false(persistence, engine, backend, *, entities):
        attempts.append(entities.shipment_id)
        if len(attempts) == 1:
            queue_capacity = next(
                definition.capacity
                for definition in persistence.store_definitions()
                if definition.name == f"{TRANSFER_OWNER}/destination_hub_queue"
            )
            assert queue_capacity is not None
            assert not any(
                item.store_name == f"{TRANSFER_OWNER}/destination_hub_queue"
                for item in persistence.store_items()
            )
            # The composed fixture uses a finite queue larger than one.
            # Occupy every slot; a single blocker cannot force backpressure.
            for ordinal in range(queue_capacity):
                engine.stores.put(
                    backend, store_name=f"{TRANSFER_OWNER}/destination_hub_queue",
                    item_id=f"destination-queue:blocker:{ordinal}",
                    value={"shipment_id": f"blocker:{ordinal}"},
                    requested_at=backend.now,
                )
                backend.run_until(backend.now)
            assert original(persistence, engine, backend, entities=entities) is False
            item_id = f"destination-queue:{entities.shipment_id}"
            assert any(intent.item_id == item_id for intent in persistence.store_put_intents())
            # Drain all blocker identities after the first slot becomes free;
            # the pending shipment put must commit without being duplicated.
            for ordinal in range(queue_capacity):
                received = engine.stores.ensure_selection(
                    backend, store_name=f"{TRANSFER_OWNER}/destination_hub_queue",
                    request_id=f"drain-destination-blocker:{ordinal}",
                    requested_at=backend.now,
                )
                assert received is not None
                assert received.item.item_id == f"destination-queue:blocker:{ordinal}"
                backend.run_until(backend.now)
            # A different worker's callback has completed the pending put.
            assert any(item.item_id == item_id for item in persistence.store_items())
            assert not any(intent.item_id == item_id for intent in persistence.store_put_intents())
            return False
        return original(persistence, engine, backend, entities=entities)

    monkeypatch.setattr(customer.logistics, "reconcile_transfer", complete_pending_put_after_false)
    result = customer.run_customer_demand_path(organization_id=TRANSFER_OWNER)
    assert len(attempts) >= 2
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"
    events = result.persistence.events()
    assert len(events) == len({event.event_id for event in events})


def test_preexisting_in_transfer_reservations_do_not_mask_unsubstantiated_false(
    monkeypatch,
):
    """A previously granted resource is not evidence of a new racing grant."""
    original_origin = customer.logistics.reconcile_origin_hub

    def dispatch_before_reconcile(persistence, engine, backend, *, entities):
        completed = original_origin(
            persistence, engine, backend, entities=entities,
        )
        if not completed:
            return completed
        shipment_id = entities.shipment_id
        transfer = engine.resources.ensure_requested(
            backend, resource_name="transfer_vehicle",
            request_id=f"transfer-vehicle:{shipment_id}", requested_at=backend.now,
        )
        assert transfer is not None
        assert customer.logistics._ensure_origin_queue_dequeue(
            persistence, engine, backend, shipment_id=shipment_id,
        )
        shipment = persistence.entity("shipment", shipment_id)
        assert shipment is not None and shipment.state == "at_origin_hub"
        customer.logistics._dispatch(
            engine, shipment, "dispatch_transfer",
            key=("test-existing-transfer", shipment_id),
        )
        dock = engine.resources.ensure_requested(
            backend, resource_name="destination_dock",
            request_id=f"destination-dock:{shipment_id}", requested_at=backend.now,
        )
        backend.run_until(backend.now)
        assert dock is not None
        return completed

    def invalid_reconcile(persistence, engine, backend, *, entities):
        shipment_id = entities.shipment_id
        assert persistence.entity("shipment", shipment_id).state == "in_transfer"
        assert engine.resources.reservation_for(f"transfer-vehicle:{shipment_id}")
        assert engine.resources.reservation_for(f"destination-dock:{shipment_id}")
        assert not any(
            intent.item_id == f"destination-queue:{shipment_id}"
            for intent in persistence.store_put_intents()
        )
        return False

    monkeypatch.setattr(customer.logistics, "reconcile_origin_hub", dispatch_before_reconcile)
    monkeypatch.setattr(customer.logistics, "reconcile_transfer", invalid_reconcile)
    # Fail fast if an incorrect DurableResourceWait enters the synchronous retry loop.
    ticks = iter(range(1000))
    monkeypatch.setattr(customer, "monotonic", lambda: next(ticks) * 100.0)
    with pytest.raises(RuntimeError, match="logistics transfer failed"):
        customer.run_customer_demand_path(organization_id=TRANSFER_OWNER)

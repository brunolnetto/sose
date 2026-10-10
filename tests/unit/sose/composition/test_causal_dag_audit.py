"""Falsify persisted causal graphs, not merely ordering of in-memory stages."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.audit import CausalAuditError, audit_causal_history
from sose.composition.model import BoundaryDelivery, BoundaryMessage
from sose.composition.trading_company_customer import run_customer_demand_path
from sose.core.events import DomainEvent
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


T0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def msg(label: str, *, at=T0, cause=None, kind=None, flow="order-A"):
    return BoundaryMessage.create(
        contract_name="test.changed", contract_version=1,
        source_domain="source", source_identity=label,
        destination_domain="destination", occurrence_key=label,
        correlation_id=flow, causation_id=cause, causation_kind=kind,
        produced_at=at, payload={"label": label},
    )


def stage(store, *messages):
    with store.transaction() as uow:
        for value in messages:
            uow.save_boundary_message(value)
            uow.save_boundary_delivery(BoundaryDelivery.pending(value))


def test_audit_reports_typed_edges_and_stable_digest_regardless_insert_order():
    a = msg("A")
    b = msg("B", at=T0 + timedelta(seconds=1), cause=a.message_id, kind="boundary")
    left, right = MemoryPersistence(), MemoryPersistence()
    stage(left, a, b)
    stage(right, b, a)
    first = audit_causal_history(left)
    second = audit_causal_history(right)
    assert first.message_count == second.message_count == 2
    assert first.typed_edges == second.typed_edges == ((a.message_id, b.message_id),)
    assert first.semantic_digest == second.semantic_digest
    assert first.applied_effects == 0


def test_typed_boundary_cause_must_exist_even_if_delivery_not_claimed():
    store = MemoryPersistence()
    stage(store, msg("orphan", cause="missing-parent", kind="boundary"))
    with pytest.raises(CausalAuditError, match="missing typed boundary parent"):
        audit_causal_history(store)


def test_causal_cycle_detected_even_when_each_message_identity_is_valid():
    store = MemoryPersistence()
    a, b = msg("A"), msg("B")
    a = replace(a, causation_id=b.message_id, causation_kind="boundary")
    b = replace(b, causation_id=a.message_id, causation_kind="boundary")
    stage(store, a, b)
    with pytest.raises(CausalAuditError, match="causal cycle"):
        audit_causal_history(store)


@pytest.mark.parametrize("breakage,expected", [
    ("future-parent", "temporal inversion"),
    ("wrong-flow", "correlation"),
])
def test_typed_cause_rejects_temporal_inversion_and_correlation_fork(breakage, expected):
    store = MemoryPersistence()
    a = msg("A", at=T0 + timedelta(seconds=2))
    b = msg(
        "B", at=T0 if breakage == "future-parent" else T0 + timedelta(seconds=3),
        cause=a.message_id, kind="boundary",
        flow="different-flow" if breakage == "wrong-flow" else "order-A",
    )
    stage(store, a, b)
    with pytest.raises(CausalAuditError, match=expected):
        audit_causal_history(store)


def test_explicit_event_cause_requires_durable_event_and_consistent_clock():
    store = MemoryPersistence()
    event = DomainEvent(
        event_id="domain-event-1", name="approved",
        entity_type="sales_order", entity_id="so-1",
        occurred_at=T0, correlation_id="order-A",
    )
    child = msg("B", at=T0 + timedelta(seconds=1), cause=event.event_id, kind="event")
    stage(store, child)
    with pytest.raises(CausalAuditError, match="missing typed domain event"):
        audit_causal_history(store)
    with store.transaction() as uow:
        uow.append_event(event)
    assert audit_causal_history(store).message_count == 1


def test_opaque_v1_causation_is_not_reinterpreted_by_prefix_or_id_collision():
    store = MemoryPersistence()
    real = msg("A")
    legacy = msg(
        "opaque", at=T0, cause=real.message_id, kind=None,
        flow="unrelated-correlation",
    )
    stage(store, real, legacy)
    graph = audit_causal_history(store)
    assert graph.typed_edges == ()
    assert graph.opaque_causes == 1


def test_customer_reference_certificates_are_causally_auditable():
    result = run_customer_demand_path()
    graph = audit_causal_history(result.persistence)
    assert graph.message_count == len(result.message_ids)
    assert graph.applied_effects == 4
    assert graph.pending_effects == 0
    assert graph.semantic_digest == audit_causal_history(result.persistence).semantic_digest


def test_deleted_business_certificate_is_detected_in_completed_chain():
    result = run_customer_demand_path()
    proof = next(x for x in result.persistence.business_effects() if x.entity_type == "shipment")
    result.persistence._state.business_effects.pop(proof.effect_id)
    with pytest.raises(CausalAuditError, match="missing applied business certificate"):
        audit_causal_history(result.persistence)


def test_audit_snapshot_is_stable_after_sqlite_restart(tmp_path, monkeypatch):
    import sose.composition.trading_company_customer as customer

    path = tmp_path / "causal-audit.sqlite"
    opened = []
    def factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    customer.run_customer_demand_path()
    original = audit_causal_history(opened[-1])
    opened[-1].close()
    with SQLiteIncrementalPersistence(path) as restored:
        rebuilt = audit_causal_history(restored)
    assert rebuilt.semantic_digest == original.semantic_digest
    assert rebuilt.applied_effects == original.applied_effects


@pytest.mark.parametrize(
    "cut",
    [
        "composition.deliver_shipment",
        "composition.complete_external_fulfillment",
        "composition.settle_customer_payment",
        "composition.post_customer_journal",
    ],
)
def test_worker_death_and_restart_preserves_normalized_causal_dag(
    tmp_path, monkeypatch, cut,
):
    import sose.composition.trading_company_customer as customer
    from sose.composition.effects import BusinessEffectService
    from sose.composition.recovery import TradingCustomerRecoveryRunner
    from sose.examples.order_to_cash import simulation as o2c
    from sose.persistence.ownership import FencedEnginePersistence
    from sose.persistence.sqlite_incremental import WriterLease

    uninterrupted = audit_causal_history(customer.run_customer_demand_path().persistence)
    path = tmp_path / "independent-replay.sqlite"
    opened = []

    def storage():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", storage)
    original = BusinessEffectService.complete

    def interrupt(self, *, effect_id, completed_at):
        command = self.persistence.command(effect_id)
        if command is not None and command.name == cut:
            raise RuntimeError("injected worker death")
        return original(self, effect_id=effect_id, completed_at=completed_at)

    monkeypatch.setattr(BusinessEffectService, "complete", interrupt)
    with pytest.raises(RuntimeError, match="injected worker death"):
        customer.run_customer_demand_path()
    monkeypatch.setattr(BusinessEffectService, "complete", original)
    opened[-1].close()
    with SQLiteIncrementalPersistence(path) as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id="causal-dag-restart",
            job_id="causal-equivalence",
            max_actions=32,
        )
        resumed = runner.run_trigger(
            trigger_id="resume-after-crash",
            now=o2c.ORIGIN + timedelta(days=2),
        )
        assert resumed.actions > 0

        def authorized_view():
            return FencedEnginePersistence(store, WriterLease(
                owner_id=runner.owner_id, epoch=store.writer_epoch(),
            ))

        assert audit_causal_history(authorized_view()).semantic_digest == uninterrupted.semantic_digest
        assert runner.run_trigger(
            trigger_id="second-check",
            now=o2c.ORIGIN + timedelta(days=2, minutes=1),
        ).actions == 0
        assert audit_causal_history(authorized_view()).semantic_digest == uninterrupted.semantic_digest


def test_causal_audit_handles_deep_linear_chains_without_recursion_failure():
    store = MemoryPersistence()
    messages = []
    parent = None
    for i in range(1200):
        record = msg(
            f"node-{i:04}", at=T0 + timedelta(seconds=i),
            cause=parent.message_id if parent else None,
            kind="boundary" if parent else None,
        )
        messages.append(record)
        parent = record
    stage(store, *messages)
    report = audit_causal_history(store)
    assert report.message_count == 1200
    assert len(report.typed_edges) == 1199


def test_consumer_receipt_cannot_exist_without_consummed_delivery():
    from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
    from sose.composition.model import BoundaryConsumption

    store = MemoryPersistence()
    service = BoundaryService(store)
    message = msg("unclaimed")
    delivery = service.publish(message)
    with store.transaction() as uow:
        uow.save_boundary_consumption(
            BoundaryConsumption.create(
                delivery=delivery, consumer_effect_id="false-effect", consumed_at=T0,
            )
        )
    with pytest.raises(CausalAuditError, match="unconsumed delivery"):
        audit_causal_history(store)


def test_consumed_message_with_invalid_receipt_is_not_causally_auditable():
    from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService

    store = MemoryPersistence()
    service = BoundaryService(store)
    message = msg("approved")
    service.publish(message)
    lease = service.claim_next(owner_id="audit-worker", now=T0,
                               lease_duration=timedelta(minutes=5))
    registry = BoundaryConsumerRegistry()
    registry.register(destination_domain="destination", contract_name="test.changed",
                      contract_version=1, handler=lambda m, u: "audit-effect")
    service.consume(lease=lease, registry=registry, now=T0)
    first = audit_causal_history(store)
    assert first.message_count == 1
    with store.transaction() as uow:
        delivery = next(iter(uow.boundary_deliveries()))
        uow._working.boundary_consumptions.pop(delivery.delivery_id)
    with pytest.raises(CausalAuditError, match="lacks durable ACK receipt"):
        audit_causal_history(store)


def test_certificate_cannot_be_reused_under_different_correlation():
    result = run_customer_demand_path()
    delivery = next(
        d for d in result.persistence._state.boundary_deliveries.values()
        if d.contract_name == "warehouse.dispatch_ready"
    )
    proof = result.persistence.business_effect(delivery.consumer_effect_id)
    assert proof is not None
    result.persistence._state.business_effects[proof.effect_id] = replace(
        proof, correlation_id="different-customer"
    )
    with pytest.raises(CausalAuditError, match="certificate contradicts"):
        audit_causal_history(result.persistence)


def test_unlinked_business_certificate_cannot_disappear_from_audit_digest():
    result = run_customer_demand_path()
    store = result.persistence
    proof = next(x for x in store.business_effects() if x.entity_type == "shipment")
    with store.transaction() as uow:
        owned = next(
            delivery for delivery in uow.boundary_deliveries()
            if delivery.message_id == proof.boundary_message_id
        )
        uow._working.boundary_deliveries.pop(owned.delivery_id)
        uow._working.boundary_consumptions.pop(owned.delivery_id)
    with pytest.raises(CausalAuditError, match="orphaned business certificate"):
        audit_causal_history(store)


def test_typed_event_with_no_correlation_cannot_parent_correlated_message():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.append_event(DomainEvent(
            event_id="without-correlation", name="accepted",
            entity_type="sales_order", entity_id="sales-1",
            occurred_at=T0, correlation_id=None,
        ))
    stage(store, msg(
        "downstream",
        at=T0 + timedelta(seconds=1),
        cause="without-correlation", kind="event", flow="order-A",
    ))
    with pytest.raises(CausalAuditError, match="typed domain event correlation mismatch"):
        audit_causal_history(store)


def test_certificate_cannot_claim_different_terminal_state_at_same_entity_version():
    """An equal-version rewrite contradicts immutable certified business truth."""
    result = run_customer_demand_path()
    store = result.persistence
    proof = next(x for x in store.business_effects() if x.entity_type == "shipment")
    with store.transaction() as uow:
        entity = uow.get_entity(proof.entity_type, proof.entity_id)
        assert entity.version == proof.entity_version
        uow.save_entity(replace(entity, state="cancelled"))
    with pytest.raises(CausalAuditError, match="certificate terminal state conflicts"):
        audit_causal_history(store)


def test_certificate_allows_later_entity_version_without_inventing_historical_state():
    """A receipt anchors its own version; a later state is not a contradiction."""
    result = run_customer_demand_path()
    store = result.persistence
    proof = next(x for x in store.business_effects() if x.entity_type == "shipment")
    baseline = audit_causal_history(store)
    with store.transaction() as uow:
        entity = uow.get_entity(proof.entity_type, proof.entity_id)
        assert entity.version == proof.entity_version
        uow.save_entity(replace(
            entity, state="archived", version=entity.version + 1,
        ))
    replay = audit_causal_history(store)
    assert replay.semantic_digest == baseline.semantic_digest
    assert replay.applied_effects == baseline.applied_effects

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

"""#644: composition publishes typed causation; untyped history stays readable (0.10: required)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.audit import audit_causal_history
from sose.composition.boundary import BoundaryService
from sose.composition.causality import matches_durable_message
from sose.composition.model import BoundaryDelivery, BoundaryMessage
from sose.composition.trading_company_customer import run_customer_demand_path
from sose.persistence.memory import MemoryPersistence

T0 = datetime(2026, 11, 18, tzinfo=timezone.utc)


def _message(label, *, cause=None, kind=None, at=T0):
    return BoundaryMessage.create(
        contract_name="test.typed", contract_version=1,
        source_domain="source", source_identity=label,
        destination_domain="destination", occurrence_key=label,
        correlation_id="flow", causation_id=cause, causation_kind=kind,
        produced_at=at, payload={"label": label},
    )


def test_current_customer_flow_leaves_no_opaque_causes():
    result = run_customer_demand_path(organization_id="test-org-typed-causation")
    report = audit_causal_history(result.persistence)
    assert report.opaque_causes == 0
    assert len(report.typed_edges) == report.message_count - 1   # one root, all else typed


def test_untyped_record_satisfies_the_same_message_rederived_typed():
    root = _message("root")
    untyped = _message("child", cause=root.message_id, at=T0 + timedelta(seconds=1))
    typed = replace(untyped, causation_kind="boundary")
    assert typed.message_id == untyped.message_id          # identity ignores the kind
    assert matches_durable_message(untyped, typed)
    assert not matches_durable_message(typed, untyped)      # never downgrade typed history
    assert not matches_durable_message(untyped, replace(typed, causation_kind="event"))
    other_produced_at = replace(typed, produced_at=T0 + timedelta(seconds=9))
    assert not matches_durable_message(untyped, other_produced_at)   # any other field differs


def _stage_untyped(store, value):
    with store.transaction() as uow:
        uow.save_boundary_message(value)
        uow.save_boundary_delivery(BoundaryDelivery.pending(value))


def test_republishing_typed_over_untyped_history_keeps_the_durable_record():
    store = MemoryPersistence()
    service = BoundaryService(store)
    root = _message("root")
    untyped = _message("child", cause=root.message_id, at=T0 + timedelta(seconds=1))
    service.publish(root)
    _stage_untyped(store, untyped)                                  # recorded before 0.9
    service.publish(replace(untyped, causation_kind="boundary"))   # recovery re-derives typed
    with store.transaction() as uow:
        assert uow.get_boundary_message(untyped.message_id).causation_kind is None


def test_publishing_a_new_untyped_caused_message_is_refused():
    """Deprecated in 0.9.0, removed in 0.10.0."""
    store = MemoryPersistence()
    service = BoundaryService(store)
    root = _message("root")
    service.publish(root)                                   # no cause: nothing to type
    service.publish(_message("ok", cause=root.message_id, kind="boundary",
                             at=T0 + timedelta(seconds=1)))
    with pytest.raises(ValueError, match="has a causation_id but no causation_kind"):
        service.publish(_message("child", cause=root.message_id, at=T0 + timedelta(seconds=2)))
    with store.transaction() as uow:
        assert uow.get_boundary_message(
            _message("child", cause=root.message_id).message_id) is None

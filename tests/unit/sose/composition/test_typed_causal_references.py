"""Fail-closed causal references for opt-in PC6 boundary messages.

Legacy untyped v1 messages retain their published history. Typed references
never interpret an absent predecessor as an external, already-applied cause.
"""
from datetime import datetime, timedelta

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.causality import CausalReference
from sose.composition.model import BoundaryMessage
from sose.core.events import DomainEvent
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


NOW = datetime(2026, 10, 9, 12)


def message(name: str, cause: CausalReference | str | None = None, correlation: str = "order-1",
            at: datetime = NOW) -> BoundaryMessage:
    return BoundaryMessage.create(
        contract_name="causal.test", contract_version=1, source_domain="warehouse",
        source_identity="order-1", destination_domain="logistics",
        occurrence_key=name, correlation_id=correlation,
        causation_id=cause.identity if isinstance(cause, CausalReference) else cause,
        causation_kind=cause.kind if isinstance(cause, CausalReference) else None,
        produced_at=at, payload={"name": name},
    )


def registry() -> BoundaryConsumerRegistry:
    handlers = BoundaryConsumerRegistry()
    handlers.register(
        destination_domain="logistics", contract_name="causal.test",
        contract_version=1, handler=lambda msg, uow: "applied-" + msg.message_id,
    )
    return handlers


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_missing_typed_boundary_parent_stays_blocked_across_restart(adapter, tmp_path):
    path = tmp_path / "causality.sqlite"
    def factory():
        return MemoryPersistence() if adapter == "memory" else SQLiteIncrementalPersistence(path)

    store = factory()
    parent = message("parent")
    child = message("child", CausalReference.boundary(parent.message_id))
    service = BoundaryService(store)
    service.publish(child)

    assert service.claim_next(owner_id="worker", now=NOW,
                              lease_duration=timedelta(minutes=5)) is None
    if adapter == "sqlite":
        store.close()
        store = factory()
        service = BoundaryService(store)
        assert service.claim_next(owner_id="worker", now=NOW,
                                  lease_duration=timedelta(minutes=5)) is None

    service.publish(parent)
    first = service.claim_next(owner_id="worker", now=NOW,
                               lease_duration=timedelta(minutes=5))
    assert first is not None and first.message_id == parent.message_id
    service.consume(lease=first, registry=registry(), now=NOW)
    second = service.claim_next(owner_id="worker", now=NOW,
                                lease_duration=timedelta(minutes=5))
    assert second is not None and second.message_id == child.message_id
    if adapter == "sqlite":
        store.close()


def test_typed_boundary_parent_cannot_change_correlation_or_reverse_clock():
    store = MemoryPersistence()
    service = BoundaryService(store)
    parent = message("parent")
    child = message("child", CausalReference.boundary(parent.message_id))
    service.publish(child)
    with pytest.raises(ValueError, match="correlation"):
        service.publish(message("parent", correlation="other"))
    with pytest.raises(ValueError, match="logical time"):
        service.publish(message("parent", at=NOW + timedelta(minutes=1)))
    assert service.claim_next(owner_id="worker", now=NOW,
                              lease_duration=timedelta(minutes=1)) is None


def test_typed_domain_event_must_exist_and_match_trace_and_time():
    store = MemoryPersistence()
    service = BoundaryService(store)
    cause = CausalReference.event("event-1")
    with pytest.raises(ValueError, match="missing.*event"):
        service.publish(message("child", cause))

    with store.transaction() as uow:
        uow.append_event(DomainEvent(
            event_id="event-1", name="shipped", entity_type="shipment",
            entity_id="s1", occurred_at=NOW, correlation_id="order-1",
        ))
    with pytest.raises(ValueError, match="correlation"):
        service.publish(message("different", cause, correlation="another"))
    with pytest.raises(ValueError, match="logical time"):
        service.publish(message("early", cause, at=NOW - timedelta(seconds=1)))
    service.publish(message("child", cause))
    lease = service.claim_next(owner_id="worker", now=NOW,
                               lease_duration=timedelta(minutes=1))
    assert lease is not None


def test_legacy_external_causation_remains_compatible():
    store = MemoryPersistence()
    service = BoundaryService(store)
    service.publish(message("legacy", "external-not-indexed"))
    assert service.claim_next(
        owner_id="legacy-worker", now=NOW,
        lease_duration=timedelta(minutes=1),
    ) is not None


def test_typed_causal_reference_rejects_invalid_kinds_and_ids():
    with pytest.raises(ValueError):
        CausalReference.boundary("")
    with pytest.raises(ValueError):
        CausalReference.event(" ")
    with pytest.raises(ValueError):
        CausalReference("unsupported", "id")
    assert CausalReference.boundary("event:legacy-id").as_message_fields() == {
        "causation_id": "event:legacy-id", "causation_kind": "boundary",
    }


def test_legacy_prefixed_cause_survives_upgrade_without_reinterpretation():
    from sose.persistence.codec import dumps, loads

    service = BoundaryService(MemoryPersistence())
    for cause in ("event:external-id", "boundary:external-id"):
        legacy = message("legacy-" + cause, cause)
        serialized = dumps(legacy)
        assert '"causation_kind"' not in serialized
        assert loads(serialized) == legacy
        service.publish(legacy)
        assert service.claim_next(
            owner_id="legacy-worker", now=NOW,
            lease_duration=timedelta(minutes=1),
        ) is not None


def test_typed_cause_is_persisted_separately_from_identifier(tmp_path):
    from sose.persistence.codec import dumps, loads

    child = message("typed", CausalReference.boundary("event:legacy-id"))
    encoded = dumps(child)
    assert '"causation_kind"' in encoded
    assert loads(encoded) == child
    path = tmp_path / "typed-restart.sqlite"
    with SQLiteIncrementalPersistence(path) as store:
        BoundaryService(store).publish(child)
    with SQLiteIncrementalPersistence(path) as store:
        assert BoundaryService(store).claim_next(
            owner_id="blocked", now=NOW,
            lease_duration=timedelta(minutes=1),
        ) is None

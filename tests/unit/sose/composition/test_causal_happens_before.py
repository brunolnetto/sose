"""Regression tests for causal order across durable boundary messages."""
from datetime import datetime, timedelta
import pytest
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryMessage, BoundaryService
from sose.core.events import Command
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2026, 10, 9, 12)

def msg(key, *, cause=None, correlation="flow", at=NOW):
    return BoundaryMessage.create(
        contract_name="test.flow", contract_version=1,
        source_domain="warehouse", source_identity="order",
        destination_domain="logistics", occurrence_key=key,
        correlation_id=correlation, causation_id=cause, produced_at=at,
        payload={"key": key},
    )

@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_child_waits_for_parent_application_and_independent_delivery_progresses(adapter, tmp_path):
    path = tmp_path / "causal.sqlite3"
    persistence = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    service = BoundaryService(persistence)
    parent = msg("parent")
    child = next(
        item for i in range(200)
        if (item := msg(f"child-{i}", cause=parent.message_id)).message_id < parent.message_id
    )
    independent = msg("independent", at=NOW + timedelta(minutes=1))
    for item in (child, parent, independent):
        service.publish(item)
    registry = BoundaryConsumerRegistry()
    def handler(message, uow):
        if message.message_id == parent.message_id:
            uow.save_command(Command(
                command_id="parent-effect", name="apply_parent",
                entity_type="shipment", entity_id="s1", due_at=NOW,
            ))
            return "parent-effect"
        return "effect-other"
    registry.register(destination_domain="logistics", contract_name="test.flow",
        contract_version=1, handler=handler)
    lease = service.claim_next(owner_id="a", now=NOW, lease_duration=timedelta(hours=1))
    assert lease is not None and lease.message_id == parent.message_id
    service.consume(lease=lease, registry=registry, now=NOW)
    lease = service.claim_next(owner_id="b", now=NOW, lease_duration=timedelta(hours=1))
    assert lease is not None and lease.message_id == independent.message_id
    service.consume(lease=lease, registry=registry, now=NOW)
    if adapter == "sqlite":
        persistence.close()
        persistence = SQLitePersistence(path)
        service = BoundaryService(persistence)
    assert service.claim_next(owner_id="c", now=NOW, lease_duration=timedelta(hours=1)) is None
    with persistence.transaction() as uow:
        uow.delete_command("parent-effect")
    lease = service.claim_next(owner_id="c", now=NOW, lease_duration=timedelta(hours=1))
    assert lease is not None and lease.message_id == child.message_id
    if adapter == "sqlite":
        persistence.close()

def test_reject_parent_mismatch_clock_regression_and_cycle():
    service = BoundaryService(MemoryPersistence())
    parent = msg("parent")
    service.publish(parent)
    with pytest.raises(ValueError, match="correlation"):
        service.publish(msg("bad", cause=parent.message_id, correlation="different"))
    with pytest.raises(ValueError, match="logical time"):
        service.publish(msg("early", cause=parent.message_id, at=NOW - timedelta(minutes=1)))
    from dataclasses import replace
    self_ref = msg("self")
    with pytest.raises(ValueError, match="causal cycle"):
        service.publish(replace(self_ref, causation_id=self_ref.message_id))

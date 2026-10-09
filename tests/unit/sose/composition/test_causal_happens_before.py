"""Regression tests for causal order across durable boundary messages."""
from datetime import datetime, timedelta
import pytest
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryMessage, BoundaryService
from sose.core.events import Command
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

NOW = datetime(2026, 10, 9, 12)

def msg(key, *, cause=None, correlation="flow", at=NOW):
    return BoundaryMessage.create(
        contract_name="test.flow", contract_version=1,
        source_domain="warehouse", source_identity="order",
        destination_domain="logistics", occurrence_key=key,
        correlation_id=correlation, causation_id=cause, produced_at=at,
        payload={"key": key},
    )

@pytest.mark.parametrize("adapter", ["memory", "sqlite", "incremental"])
def test_child_waits_for_parent_application_and_independent_delivery_progresses(adapter, tmp_path):
    path = tmp_path / "causal.sqlite3"
    persistence = (MemoryPersistence() if adapter == "memory" else
        SQLitePersistence(path) if adapter == "sqlite" else SQLiteIncrementalPersistence(path))
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
    if adapter != "memory":
        persistence.close()
        persistence = (SQLitePersistence(path) if adapter == "sqlite"
            else SQLiteIncrementalPersistence(path))
        service = BoundaryService(persistence)
    assert service.claim_next(owner_id="c", now=NOW, lease_duration=timedelta(hours=1)) is None
    with persistence.transaction() as uow:
        uow.delete_command("parent-effect")
    lease = service.claim_next(owner_id="c", now=NOW, lease_duration=timedelta(hours=1))
    assert lease is not None and lease.message_id == child.message_id
    if adapter != "memory":
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


def test_ack_rechecks_parent_that_arrives_after_child_claim():
    from sose.composition.boundary import StaleBoundaryClaimError
    store = MemoryPersistence()
    service = BoundaryService(store)
    parent = msg("late-parent")
    child = msg("early-claim", cause=parent.message_id)
    service.publish(child)  # Source may be an external event until its boundary record arrives.
    registry = BoundaryConsumerRegistry()
    registry.register(destination_domain="logistics", contract_name="test.flow",
        contract_version=1, handler=lambda message, uow: "effect-" + message.message_id)
    child_lease = service.claim_next(
        owner_id="worker-child", now=NOW, lease_duration=timedelta(hours=1),
    )
    assert child_lease is not None and child_lease.message_id == child.message_id
    service.publish(parent)
    # Critical TOCTOU: parent was not persisted when the child was leased.
    with pytest.raises(StaleBoundaryClaimError, match="causal predecessor"):
        service.consume(lease=child_lease, registry=registry, now=NOW)
    assert service.consumption(child_lease.delivery_id) is None
    parent_lease = service.claim_next(
        owner_id="worker-parent", now=NOW, lease_duration=timedelta(hours=1),
    )
    assert parent_lease is not None and parent_lease.message_id == parent.message_id
    service.consume(lease=parent_lease, registry=registry, now=NOW)
    assert service.consume(
        lease=child_lease, registry=registry, now=NOW
    ).consumer_effect_id == "effect-" + child.message_id


def test_reject_indirect_cycle_after_out_of_order_publication():
    from dataclasses import replace
    service = BoundaryService(MemoryPersistence())
    parent = msg("late-parent")
    child = msg("published-child", cause=parent.message_id)
    service.publish(child)
    with pytest.raises(ValueError, match="causal cycle"):
        service.publish(replace(parent, causation_id=child.message_id))
    # External event causes are intentionally not looked up as boundary messages.
    assert service.publish(msg("external-event", cause="domain-event-123"))


def test_late_parent_conflict_must_not_poison_the_durable_queue():
    from dataclasses import replace
    store = MemoryPersistence()
    service = BoundaryService(store)
    parent = msg("late-parent")
    child = msg("early-child", cause=parent.message_id)
    service.publish(child)
    # The child arrived first, but a contradictory ancestor cannot become
    # immutable durable truth and poison all future claim attempts.
    with pytest.raises(ValueError, match="correlation"):
        service.publish(replace(parent, correlation_id="other-flow"))
    with pytest.raises(ValueError, match="logical time"):
        service.publish(replace(parent, produced_at=NOW + timedelta(minutes=1)))
    with store.transaction() as uow:
        assert uow.get_boundary_message(parent.message_id) is None
    service.publish(parent)
    lease = service.claim_next(owner_id="parent-worker", now=NOW,
        lease_duration=timedelta(hours=1))
    assert lease is not None and lease.message_id == parent.message_id


def test_cannot_publish_ancestor_after_descendant_was_already_acknowledged():
    store = MemoryPersistence()
    service = BoundaryService(store)
    parent = msg("missing-parent")
    child = msg("consumed-as-external", cause=parent.message_id)
    service.publish(child)
    registry = BoundaryConsumerRegistry()
    registry.register(destination_domain="logistics", contract_name="test.flow",
        contract_version=1, handler=lambda message, uow: "effect-external")
    lease = service.claim_next(owner_id="early-worker", now=NOW,
        lease_duration=timedelta(hours=1))
    assert lease is not None
    service.consume(lease=lease, registry=registry, now=NOW)
    # The sequence is irreparable: accepting a new boundary predecessor would
    # retroactively assert an untrue causal order.
    with pytest.raises(ValueError, match="already consumed"):
        service.publish(parent)
    with store.transaction() as uow:
        assert uow.get_boundary_message(parent.message_id) is None


def test_idempotent_parent_republication_after_completed_descendant():
    service = BoundaryService(MemoryPersistence())
    parent = msg("normal-parent")
    child = msg("normal-child", cause=parent.message_id)
    original_delivery = service.publish(parent)
    service.publish(child)
    registry = BoundaryConsumerRegistry()
    registry.register(destination_domain="logistics", contract_name="test.flow",
        contract_version=1, handler=lambda message, uow: "effect-" + message.message_id)
    for expected in (parent, child):
        lease = service.claim_next(owner_id="worker", now=NOW,
            lease_duration=timedelta(hours=1))
        assert lease is not None and lease.message_id == expected.message_id
        service.consume(lease=lease, registry=registry, now=NOW)
    assert service.publish(parent) == service.delivery(original_delivery.delivery_id)


def test_two_independent_sqlite_workers_cannot_race_causal_ancestry(tmp_path):
    from sose.composition.boundary import StaleBoundaryClaimError
    path = tmp_path / "two-workers.sqlite3"
    left = SQLiteIncrementalPersistence(path)
    right = SQLiteIncrementalPersistence(path)
    try:
        consumer_a = BoundaryService(left)
        consumer_b = BoundaryService(right)
        parent = msg("worker-parent")
        child = next(
            candidate for i in range(200)
            if (candidate := msg(f"worker-child-{i}", cause=parent.message_id)).message_id
            < parent.message_id
        )
        consumer_a.publish(child)
        consumer_a.publish(parent)
        first = consumer_a.claim_next(
            owner_id="left", now=NOW, lease_duration=timedelta(seconds=2),
        )
        assert first is not None and first.message_id == parent.message_id
        # The competing worker must not steal a leased ancestor or execute a
        # descendant with an earlier UUID at the same logical time.
        assert consumer_b.claim_next(
            owner_id="right", now=NOW + timedelta(seconds=1),
            lease_duration=timedelta(seconds=2),
        ) is None
        recovered = consumer_b.claim_next(
            owner_id="right", now=NOW + timedelta(seconds=3),
            lease_duration=timedelta(minutes=1),
        )
        assert recovered is not None
        assert recovered.message_id == parent.message_id
        assert recovered.epoch == first.epoch + 1
        registry = BoundaryConsumerRegistry()
        def parent_intent(message, uow):
            uow.save_command(Command(
                command_id="causal-worker-effect", name="apply_parent",
                entity_type="shipment", entity_id="s1", due_at=NOW,
            ))
            return "causal-worker-effect"
        registry.register(destination_domain="logistics", contract_name="test.flow",
            contract_version=1, handler=parent_intent)
        with pytest.raises(StaleBoundaryClaimError):
            consumer_a.consume(
                lease=first, registry=registry, now=NOW + timedelta(seconds=3),
            )
        consumer_b.consume(
            lease=recovered, registry=registry, now=NOW + timedelta(seconds=3),
        )
        assert consumer_a.claim_next(
            owner_id="left", now=NOW + timedelta(seconds=4),
            lease_duration=timedelta(minutes=1),
        ) is None
        with right.transaction() as uow:
            uow.delete_command("causal-worker-effect")
        child_lease = consumer_a.claim_next(
            owner_id="left", now=NOW + timedelta(seconds=5),
            lease_duration=timedelta(minutes=1),
        )
        assert child_lease is not None and child_lease.message_id == child.message_id
    finally:
        right.close()
        left.close()

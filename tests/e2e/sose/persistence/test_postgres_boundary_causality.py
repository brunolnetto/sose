"""PostgreSQL causal transactions must serialize boundary claims across workers.

Unlike generic disjoint domain writes, parent publication and child claims must
not read different revisions and then commit as if they shared a causal order.
"""
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import uuid4
import os

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.model import BoundaryMessage
from sose.core.events import Command
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def message(occurrence: str, cause: str | None = None):
    return BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse", source_identity="order-1",
        destination_domain="logistics", occurrence_key=occurrence,
        correlation_id="causal-order-1", causation_id=cause,
        causation_kind="boundary" if cause is not None else None,
        produced_at=NOW, payload={"order_id": "order-1"},
    )


def test_pg_parent_publish_blocks_competing_claim_until_commit(monkeypatch):
    assert DSN is not None
    namespace = "causal_" + uuid4().hex[:16]
    with PostgresPersistence(DSN, namespace=namespace) as left, \
         PostgresPersistence(DSN, namespace=namespace) as right:
        parent = message("parent")
        child = message("child", parent.message_id)
        BoundaryService(left).publish(child)

        entering = Event()
        release = Event()
        claim_started = Event()
        claim_done = Event()
        errors = []
        result = []
        validate_original = BoundaryService._validate_causation

        def blocked_publish_validation(value, uow):
            if value.message_id == parent.message_id:
                entering.set()
                if not release.wait(10):
                    raise TimeoutError("parent publish not released")
            return validate_original(value, uow)

        monkeypatch.setattr(
            BoundaryService, "_validate_causation", staticmethod(blocked_publish_validation),
        )

        def publisher():
            try:
                BoundaryService(left).publish(parent)
            except BaseException as exc:
                errors.append(exc)

        def consumer():
            claim_started.set()
            try:
                result.append(BoundaryService(right).claim_next(
                    owner_id="worker-b", now=NOW,
                    lease_duration=timedelta(hours=1),
                    destination_domain="logistics",
                ))
            except BaseException as exc:
                errors.append(exc)
            finally:
                claim_done.set()

        first = Thread(target=publisher)
        second = Thread(target=consumer)
        first.start()
        assert entering.wait(10)
        second.start()
        assert claim_started.wait(10)
        try:
            # A boundary claim must not snapshot an in-progress parent publish.
            assert not claim_done.wait(0.4)
        finally:
            release.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        assert errors == []
        assert len(result) == 1
        assert result[0] is not None
        assert result[0].message_id == parent.message_id


def test_pg_parent_business_effect_blocks_descendants_between_workers():
    assert DSN is not None
    namespace = "causal_" + uuid4().hex[:16]
    with PostgresPersistence(DSN, namespace=namespace) as left, \
         PostgresPersistence(DSN, namespace=namespace) as right:
        service = BoundaryService(left)
        parent = message("root")
        child = message("dependent", parent.message_id)
        service.publish(child)
        service.publish(parent)

        first = service.claim_next(
            owner_id="worker-a", now=NOW, lease_duration=timedelta(minutes=5),
        )
        assert first.message_id == parent.message_id
        assert BoundaryService(right).claim_next(
            owner_id="worker-b", now=NOW, lease_duration=timedelta(minutes=5),
        ) is None
        handlers = BoundaryConsumerRegistry()

        def stage_effect(message, uow):
            uow.save_command(Command(
                command_id="pg-parent-effect", name="do_business",
                entity_type="shipment", entity_id="shipment-1", due_at=NOW,
            ))
            return "pg-parent-effect"

        handlers.register(
            destination_domain="logistics", contract_name="warehouse.dispatch_ready",
            contract_version=1, handler=stage_effect,
        )
        service.consume(lease=first, registry=handlers, now=NOW)
        assert BoundaryService(right).claim_next(
            owner_id="worker-b", now=NOW, lease_duration=timedelta(minutes=5),
        ) is None

        with left.transaction() as uow:
            uow.delete_command("pg-parent-effect")
        child_lease = BoundaryService(right).claim_next(
            owner_id="worker-b", now=NOW, lease_duration=timedelta(minutes=5),
        )
        assert child_lease is not None and child_lease.message_id == child.message_id

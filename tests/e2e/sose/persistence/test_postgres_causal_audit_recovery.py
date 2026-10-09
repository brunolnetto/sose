"""PostgreSQL causal audit must see whole certified commits under worker races.

A physical delivery ACK is not proof. The fingerprint is stable across DB
reconstruction and independent connection leases, not tied to write ordering.
"""
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import uuid4
import os

import pytest

from sose.composition.audit import audit_causal_history
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.effects import BusinessEffectService
from sose.composition.model import BoundaryMessage
from sose.core.events import Command
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryUnitOfWork
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def dispatch():
    return BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse_fulfillment", source_identity="wf-1",
        destination_domain="logistics", occurrence_key="pg-audit-dispatch",
        correlation_id="pg-audit-flow", causation_id=None, produced_at=NOW,
        payload={"shipment_id": "shipment-pg-1", "fulfillment_order_id": "wf-1"},
    )


def registry():
    consumers = BoundaryConsumerRegistry()

    def accept(message, uow):
        uow.save_command(Command(
            command_id="pg-audit-delivery-effect",
            name="composition.deliver_shipment",
            entity_type="shipment", entity_id="shipment-pg-1",
            due_at=NOW, issued_at=NOW,
            causation_id=message.message_id,
            correlation_id=message.correlation_id,
        ))
        return "pg-audit-delivery-effect"

    consumers.register(
        destination_domain="logistics", contract_name="warehouse.dispatch_ready",
        contract_version=1, handler=accept,
    )
    return consumers


def test_pg_audit_waits_for_atomic_certificate_commit_then_survives_restart(monkeypatch):
    assert DSN is not None
    namespace = "audit_" + uuid4().hex[:16]
    with (
        PostgresPersistence(DSN, namespace=namespace) as worker,
        PostgresPersistence(DSN, namespace=namespace) as independent,
    ):
        service = BoundaryService(worker)
        message = dispatch()
        service.publish(message)
        lease = service.claim_next(
            owner_id="worker-a", now=NOW, lease_duration=timedelta(hours=1),
            destination_domain="logistics",
        )
        service.consume(lease=lease, registry=registry(), now=NOW)
        with worker.transaction() as uow:
            uow.save_entity(Entity(
                id="shipment-pg-1", entity_type="shipment",
                state="delivered", version=9,
            ))
        before = audit_causal_history(independent)
        assert before.applied_effects == 0
        assert before.pending_effects == 1

        entered = Event()
        release = Event()
        audit_started = Event()
        audit_finished = Event()
        proofs = []
        reports = []
        errors = []
        original_save = MemoryUnitOfWork.save_business_effect

        def hold_certificate(self, receipt):
            if receipt.effect_id == "pg-audit-delivery-effect":
                entered.set()
                if not release.wait(15):
                    raise TimeoutError("test certificate writer never released")
            return original_save(self, receipt)

        monkeypatch.setattr(
            MemoryUnitOfWork, "save_business_effect", hold_certificate,
        )

        def certify():
            try:
                proofs.append(BusinessEffectService(worker).complete(
                    effect_id="pg-audit-delivery-effect", completed_at=NOW,
                ))
            except BaseException as error:
                errors.append(error)

        def read_concurrently():
            audit_started.set()
            try:
                reports.append(audit_causal_history(independent))
            except BaseException as error:
                errors.append(error)
            finally:
                audit_finished.set()

        writer = Thread(target=certify)
        reader = Thread(target=read_concurrently)
        writer.start()
        try:
            assert entered.wait(10)
            reader.start()
            assert audit_started.wait(10)
            # The snapshot must not overtake a still-uncommitted certificate.
            assert not audit_finished.wait(0.4)
        finally:
            release.set()
        writer.join(timeout=15)
        reader.join(timeout=15)
        assert not writer.is_alive() and not reader.is_alive()
        assert errors == []
        assert len(proofs) == 1
        assert len(reports) == 1
        assert reports[0].applied_effects == 1
        assert reports[0].pending_effects == 0
        assert reports[0].semantic_digest != before.semantic_digest
        assert worker.command("pg-audit-delivery-effect") is None
        assert independent.business_effect("pg-audit-delivery-effect") == proofs[0]
        stable = reports[0]
        # A lease-retry on a completed transport does not change semantic truth.
        assert BoundaryService(independent).claim_next(
            owner_id="worker-b", now=NOW + timedelta(days=2),
            lease_duration=timedelta(minutes=5), destination_domain="logistics",
        ) is None
        assert audit_causal_history(independent).semantic_digest == stable.semantic_digest

    with PostgresPersistence(DSN, namespace=namespace) as restarted:
        replay = audit_causal_history(restarted)
        assert replay == stable
        assert BusinessEffectService(restarted).get("pg-audit-delivery-effect") == proofs[0]


def test_pg_audit_stable_across_independent_restart_and_physical_claim_order():
    assert DSN is not None
    namespace = "audit_" + uuid4().hex[:16]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        upstream = dispatch()
        dependent = BoundaryMessage.create(
            contract_name="logistics.delivery_completed", contract_version=1,
            source_domain="logistics", source_identity="shipment-pg-1",
            destination_domain="order_to_cash", occurrence_key="pg-audit-completed",
            correlation_id=upstream.correlation_id,
            causation_kind="boundary", causation_id=upstream.message_id,
            produced_at=NOW + timedelta(seconds=1),
            payload={"shipment_id": "shipment-pg-1", "order_id": "sales-pg-1"},
        )
        service = BoundaryService(store)
        service.publish(dependent)
        service.publish(upstream)
        before = audit_causal_history(store)
        assert before.message_count == 2
        assert before.typed_edges == ((upstream.message_id, dependent.message_id),)
        # Claiming a predecessor is a physical lease transition, not business application.
        claim = service.claim_next(
            owner_id="worker-a", now=NOW, lease_duration=timedelta(minutes=1),
            destination_domain="logistics",
        )
        assert claim is not None and claim.message_id == upstream.message_id
        during = audit_causal_history(store)
        assert during.semantic_digest == before.semantic_digest

    with PostgresPersistence(DSN, namespace=namespace) as reopened:
        assert audit_causal_history(reopened) == during
        # The stale lease expires, but semantic history still cannot change.
        claim = BoundaryService(reopened).claim_next(
            owner_id="worker-b", now=NOW + timedelta(minutes=2),
            lease_duration=timedelta(minutes=1),
            destination_domain="logistics",
        )
        assert claim is not None
        assert audit_causal_history(reopened).semantic_digest == before.semantic_digest

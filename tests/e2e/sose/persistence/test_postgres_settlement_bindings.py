"""Two PostgreSQL workers must not double-bind a payment by commit race."""
import os
from threading import Event, Thread
from uuid import uuid4

import pytest

from sose.composition.bindings import CustomerSettlementBinding, SettlementBindingService
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryUnitOfWork
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")


def test_pg_competing_bindings_serialize_and_reject_second_payment_owner(monkeypatch):
    assert DSN is not None
    namespace = "settlement_" + uuid4().hex[:16]
    with (
        PostgresPersistence(DSN, namespace=namespace) as first,
        PostgresPersistence(DSN, namespace=namespace) as second,
    ):
        with first.transaction() as uow:
            for kind, name in (
                ("sales_order", "order-1"),
                ("sales_order", "order-2"),
                ("card_payment", "card-same"),
                ("journal_entry", "journal-1"),
                ("journal_entry", "journal-2"),
            ):
                uow.save_entity(Entity(
                    id=name, entity_type=kind, state="submitted",
                    attributes={"amount": 250.0, "currency": "USD"},
                ))

        make = lambda order_id, journal_id: CustomerSettlementBinding.create(
            order_id=order_id, payment_id="card-same", journal_id=journal_id,
            amount=250.0, currency="USD", correlation_id=f"flow:{order_id}",
        )
        writer_inside_uow = Event()
        writer_release = Event()
        other_started = Event()
        other_done = Event()
        errors = []
        winners = []

        original = MemoryUnitOfWork.save_customer_settlement_binding

        def pause_before_commit(self, value):
            if value.order_id == "order-1":
                writer_inside_uow.set()
                if not writer_release.wait(10):
                    raise TimeoutError("first settlement binding not released")
            return original(self, value)

        monkeypatch.setattr(
            MemoryUnitOfWork, "save_customer_settlement_binding", pause_before_commit,
        )

        def publish_one():
            try:
                winners.append(SettlementBindingService(first).bind(make("order-1", "journal-1")))
            except BaseException as exc:
                errors.append(exc)

        def contest():
            other_started.set()
            try:
                SettlementBindingService(second).bind(make("order-2", "journal-2"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                other_done.set()

        t1 = Thread(target=publish_one)
        t2 = Thread(target=contest)
        t1.start()
        assert writer_inside_uow.wait(10)
        t2.start()
        assert other_started.wait(10)
        try:
            assert not other_done.wait(0.4)
        finally:
            writer_release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()
        assert len(winners) == 1 and winners[0].order_id == "order-1"
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert "already bound" in str(errors[0])
        recovered = SettlementBindingService(second)
        assert recovered.for_payment("card-same") == winners[0]
        assert recovered.for_order("order-2") is None

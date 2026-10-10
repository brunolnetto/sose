"""PC6 PostgreSQL multi-customer organization fixture identity falsification.

This is a prerequisite for real domain-engine multi-flow equivalence.
"""
import os
from uuid import uuid4

import pytest

from sose.composition.bindings import CustomerSettlementBinding, SettlementBindingService
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.cards_payments import simulation as payments
from sose.examples.logistics import simulation as logistics
from sose.examples.record_to_report import simulation as r2r
from sose.examples.warehouse_management import simulation as warehouse
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


def test_pg_two_scoped_pc6_organization_fixtures_do_not_alias_on_restart():
    assert DSN is not None
    namespace = "pc6fixtures_" + uuid4().hex[:12]
    identities = {}
    with PostgresPersistence(DSN, namespace=namespace) as writer:
        for key in ("customer-a", "customer-b"):
            order = o2c.seed_reference(writer, amount=250.0, instance_key=key)
            payment = payments.seed_reference(writer, amount=250.0, instance_key=key)
            accounting = r2r.seed_reference(writer, amount=250.0, instance_key=key)
            shipment = logistics.seed_reference(writer, instance_key=key)
            stock = warehouse.seed_reference(
                writer, instance_key=key, origin_on_hand=20.0,
                transfer_quantity=1.0,
            )
            identities[key] = (order, payment, accounting, shipment, stock)
            SettlementBindingService(writer).bind(CustomerSettlementBinding.create(
                order_id=order.order_id, payment_id=payment.payment_id,
                journal_id=accounting.journal_id, amount=250.0, currency="USD",
                correlation_id=f"pc6:{key}",
            ))
        assert len(writer.business_effects()) == 0

    with PostgresPersistence(DSN, namespace=namespace) as reader:
        first, second = identities.values()
        for field, kind in (
            ("order_id", "sales_order"),
            ("payment_id", "card_payment"),
            ("journal_id", "journal_entry"),
            ("shipment_id", "shipment"),
            ("origin_stock_id", "warehouse_management_stock"),
        ):
            left = next(getattr(x, field) for x in first if hasattr(x, field))
            right = next(getattr(x, field) for x in second if hasattr(x, field))
            assert left != right, (kind, left)
            assert reader.entity(kind, left) is not None
            assert reader.entity(kind, right) is not None
        for key, (order, payment, accounting, _, _) in identities.items():
            binding = SettlementBindingService(reader).for_order(order.order_id)
            assert binding is not None
            assert binding.payment_id == payment.payment_id
            assert binding.journal_id == accounting.journal_id
            assert binding.correlation_id == f"pc6:{key}"
            assert SettlementBindingService(reader).for_payment(payment.payment_id) == binding
            assert SettlementBindingService(reader).for_journal(accounting.journal_id) == binding

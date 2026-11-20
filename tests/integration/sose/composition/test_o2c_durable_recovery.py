"""PC6 v1 falsification: O2C payment egress rejects tampered durable output.

The crash/restart recovery evidence lives in
tests/e2e/sose/persistence/test_postgres_o2c_durable_recovery.py (#634).
"""
import pytest

from sose.composition import trading_company_customer as customer
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def test_payment_egress_rejects_tampered_durable_existing_output(tmp_path, monkeypatch):
    path = tmp_path / "tampered.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    customer.run_customer_demand_path(organization_id="test-org-o2c-durable-recovery")
    opened[-1].close()

    from sose.persistence.memory import MemoryPersistence
    with SQLiteIncrementalPersistence(path) as recovered:
        store = MemoryPersistence()
        store._state = recovered._state
        with store.transaction() as uow:
            payment = next(
                uow.get_boundary_message(d.message_id)
                for d in uow.boundary_deliveries()
                if d.contract_name == "o2c.payment_requested"
            )
            assert payment is not None
        assert _owner_scoped_reconcile_invoiced_o2c_egress(store) == (payment,)
        from sose.composition.model import BoundaryMessage
        corrupt = BoundaryMessage.create(
            contract_name=payment.contract_name, contract_version=payment.contract_version,
            source_domain=payment.source_domain, source_identity=payment.source_identity,
            destination_domain=payment.destination_domain,
            occurrence_key=payment.occurrence_key, correlation_id=payment.correlation_id,
            causation_id=payment.causation_id, produced_at=payment.produced_at,
            payload={**payment.payload(), "payment_id": "wrong"},
        )
        # An adversarial mismatch is injected below the normal immutable-write
        # contract, representing corrupted warehouse recovery state.
        store._state.boundary_messages[corrupt.message_id] = corrupt
        with pytest.raises(ValueError, match="conflicts"):
            _owner_scoped_reconcile_invoiced_o2c_egress(store)


def _owner_scoped_reconcile_invoiced_o2c_egress(store, **kwargs):
    """The fixture declares exactly one operational owner, not a global default."""
    owners = {
        receipt.correlation_id: "test-org-o2c-durable-recovery"
        for receipt in store.business_effects()
    }
    assert owners, "fixture must supply a certified owner-bound flow"
    return customer.reconcile_invoiced_o2c_egress(store, organization_owners=owners, **kwargs)

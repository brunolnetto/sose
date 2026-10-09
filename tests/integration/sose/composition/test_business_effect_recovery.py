"""End-to-end and crash/restart evidence for applied-effect certificates."""
from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition import trading_company_replenishment as replenishment
from sose.composition.effects import BusinessEffectService
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def test_customer_reference_has_four_distinct_business_completion_certificates():
    result = customer.run_customer_demand_path()
    effects = result.persistence.business_effects()
    assert len(effects) == 4
    assert {x.entity_type for x in effects} == {
        "shipment", "sales_order", "card_payment", "journal_entry",
    }
    assert {x.terminal_state for x in effects} == {
        "delivered", "invoiced", "settled", "posted",
    }
    assert {x.correlation_id for x in effects} == {result.correlation_id}
    assert len({x.effect_id for x in effects}) == 4
    with result.persistence.transaction() as uow:
        for proof in effects:
            evidence = uow.get_boundary_message(proof.boundary_message_id)
            assert evidence is not None
            assert evidence.correlation_id == proof.correlation_id
            entity = uow.get_entity(proof.entity_type, proof.entity_id)
            assert entity is not None and entity.state == proof.terminal_state
            assert entity.version == proof.entity_version
            assert uow.get_command(proof.effect_id) is None
            assert uow.get_business_effect(proof.effect_id) == proof


def test_replenishment_journal_uses_same_receipt_contract():
    result = replenishment.run_replenishment_path()
    effects = result.persistence.business_effects()
    assert len(effects) == 1
    assert effects[0].entity_type == "journal_entry"
    assert effects[0].entity_id == result.journal_id
    assert effects[0].terminal_state == "posted"
    assert effects[0].correlation_id == result.correlation_id


@pytest.mark.parametrize(
    "effect_name,expected_type,expected_state",
    [
        ("composition.deliver_shipment", "shipment", "delivered"),
        ("composition.complete_external_fulfillment", "sales_order", "invoiced"),
        ("composition.settle_customer_payment", "card_payment", "settled"),
        ("composition.post_customer_journal", "journal_entry", "posted"),
    ],
)
def test_worker_death_after_business_effect_before_proof_is_idempotent(
    tmp_path, monkeypatch, effect_name, expected_type, expected_state,
):
    db = tmp_path / "crash-between-business-and-certificate.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(db)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    original_complete = BusinessEffectService.complete

    def crash_before_certificate(self, *, effect_id, completed_at):
        command = self.persistence.command(effect_id)
        if command is not None and command.name == effect_name:
            raise RuntimeError("worker killed before certificate commit")
        return original_complete(self, effect_id=effect_id, completed_at=completed_at)

    monkeypatch.setattr(BusinessEffectService, "complete", crash_before_certificate)
    with pytest.raises(RuntimeError, match="worker killed before certificate commit"):
        customer.run_customer_demand_path()
    monkeypatch.setattr(BusinessEffectService, "complete", original_complete)
    opened[-1].close()

    with SQLiteIncrementalPersistence(db) as store:
        target = next(
            e for e in store.entities() if e.entity_type == expected_type
        )
        assert target.state == expected_state
        before_version = target.version
        assert not any(
            x.entity_type == expected_type for x in store.business_effects()
        )
        worker = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="recover-business-proof",
            job_id="business-proof-recovery", max_actions=16,
        )
        first = worker.run_trigger(
            trigger_id="resume-effects", now=o2c.ORIGIN + timedelta(days=2),
        )
        assert first.actions >= 1
        evidence = [
            x for x in store.business_effects()
            if x.entity_type == expected_type and x.entity_id == target.id
        ]
        assert len(evidence) == 1
        assert evidence[0].terminal_state == expected_state
        assert store.entity(expected_type, target.id).version == before_version
        second = worker.run_trigger(
            trigger_id="retry-effects", now=o2c.ORIGIN + timedelta(days=2, minutes=1),
        )
        assert second.actions == 0
        assert store.business_effect(evidence[0].effect_id) == evidence[0]

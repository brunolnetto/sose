"""End-to-end evidence for applied-effect certificates.

The crash/restart recovery evidence lives in
tests/e2e/sose/persistence/test_postgres_business_effect_recovery.py (#634).
"""
from sose.composition import trading_company_customer as customer
from sose.composition import trading_company_replenishment as replenishment


REPLENISHMENT_OWNER_ID = "trading-company-replenishment-reference"

def test_customer_reference_has_four_distinct_business_completion_certificates():
    result = customer.run_customer_demand_path(organization_id="test-org-business-effect-recovery")
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
    result = replenishment.run_replenishment_path(owner_id=REPLENISHMENT_OWNER_ID)
    effects = result.persistence.business_effects()
    assert len(effects) == 1
    assert effects[0].entity_type == "journal_entry"
    assert effects[0].entity_id == result.journal_id
    assert effects[0].terminal_state == "posted"
    assert effects[0].correlation_id == result.correlation_id

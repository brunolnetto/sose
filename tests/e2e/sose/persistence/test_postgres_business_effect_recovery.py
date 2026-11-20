"""Crash/restart evidence for applied-effect certificates on PostgreSQL.

Ported from the deprecated policy-less SQLite recovery mode (#634). The runner
uses a neutral resource policy: it maps only the certified replenishment
journal intent, which the customer path never issues, so every customer effect
keeps its historical (unmapped) admission semantics.
"""
from datetime import timedelta
import os
from uuid import uuid4

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.effects import BusinessEffectService
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")

ORGANIZATION = "test-org-business-effect-recovery"
# Certified, but never exercised by the customer demand path.
NEUTRAL_POLICY = {
    "composition.post_replenishment_journal": ResourcePoolContract(
        resource_type="journal_clerk", pool_id="business-effect-neutral",
        scope="shared", capacity=1,
    ),
}


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
    monkeypatch, effect_name, expected_type, expected_state,
):
    namespace = "business_proof_" + uuid4().hex[:12]
    opened = []

    def factory():
        store = PostgresPersistence(DSN, namespace=namespace)
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
        customer.run_customer_demand_path(organization_id=ORGANIZATION)
    monkeypatch.setattr(BusinessEffectService, "complete", original_complete)
    opened[-1].close()

    # Restart: a fresh connection rehydrates authoritative PostgreSQL truth.
    with PostgresPersistence(DSN, namespace=namespace) as store:
        target = next(
            e for e in store.entities() if e.entity_type == expected_type
        )
        assert target.state == expected_state
        before_version = target.version
        assert not any(
            x.entity_type == expected_type for x in store.business_effects()
        )
        # Reconstitute the flow from persisted delivery evidence, but take its
        # owning organization from the explicit fixture identity.
        with store.transaction() as uow:
            correlations = {
                message.correlation_id
                for delivery in uow.boundary_deliveries()
                if (message := uow.get_boundary_message(delivery.message_id)) is not None
            }
        assert len(correlations) == 1
        flow_id = next(iter(correlations))
        worker = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="recover-business-proof",
            organization_owners={flow_id: ORGANIZATION},
            resource_policies=NEUTRAL_POLICY,
            resource_organizations={flow_id: ORGANIZATION},
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

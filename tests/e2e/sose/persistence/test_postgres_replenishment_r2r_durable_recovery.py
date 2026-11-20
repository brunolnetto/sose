"""Replenishment shares the R2R contract but uses a distinct durable intent.

PostgreSQL policy-mode port of the deprecated SQLite recovery (#634); restart
is a fresh PostgresPersistence connection on the same namespace.
"""
from datetime import timedelta
import os
from uuid import uuid4

import pytest

from sose.composition import trading_company_replenishment as replenishment
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.p2p import simulation as p2p
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")

# The replenishment path issues post_replenishment_journal, so the neutral
# mapping uses a certified customer intent this path never issues: every
# replenishment effect keeps its historical (unmapped) admission semantics (#634).
NEUTRAL_POLICY = {
    "composition.deliver_shipment": ResourcePoolContract(
        resource_type="pickup_courier", pool_id="replenishment-r2r-neutral",
        scope="shared", capacity=1,
    ),
}


REPLENISHMENT_OWNER_ID = "trading-company-replenishment-reference"

@pytest.mark.parametrize(
    "cut,expected",
    [
        ("ack_before_business", "drafted"),
        ("submitted", "submitted"),
        ("posted_before_command_checkpoint", "posted"),
    ],
)
def test_replenishment_r2r_accepted_effect_resumes_after_worker_death(
    monkeypatch, cut, expected,
):
    namespace = "replenishment_r2r_" + uuid4().hex[:12]
    opened = []

    def factory():
        store = PostgresPersistence(DSN, namespace=namespace)
        opened.append(store)
        return store

    monkeypatch.setattr(replenishment, "MemoryPersistence", factory)
    original_execute = replenishment._execute_accounting
    original_dispatch = replenishment.r2r._dispatch

    def crash_before(*args, **kwargs):
        if cut == "ack_before_business":
            raise RuntimeError("replenishment accounting worker killed")
        return original_execute(*args, **kwargs)

    def crash_after_dispatch(*args, **kwargs):
        result = original_dispatch(*args, **kwargs)
        if (cut == "submitted" and args[2] == "submit") or (
            cut == "posted_before_command_checkpoint" and args[2] == "post"
        ):
            raise RuntimeError("replenishment accounting worker killed")
        return result

    monkeypatch.setattr(replenishment, "_execute_accounting", crash_before)
    monkeypatch.setattr(replenishment.r2r, "_dispatch", crash_after_dispatch)
    with pytest.raises(RuntimeError, match="replenishment accounting worker killed"):
        replenishment.run_replenishment_path(owner_id=REPLENISHMENT_OWNER_ID)
    monkeypatch.setattr(replenishment, "_execute_accounting", original_execute)
    monkeypatch.setattr(replenishment.r2r, "_dispatch", original_dispatch)
    opened[-1].close()

    with PostgresPersistence(DSN, namespace=namespace) as store:
        journal = next(e for e in store.entities() if e.entity_type == "journal_entry")
        assert journal.state == expected
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
            persistence=store, owner_id="replenishment-r2r-restart",
            organization_owners={flow_id: "trading-company-replenishment-reference"},
            resource_policies=NEUTRAL_POLICY,
            resource_organizations={flow_id: "trading-company-replenishment-reference"},
            job_id="replenishment-r2r-recovery", max_actions=16,
        )
        result = worker.run_trigger(
            trigger_id="replenishment-r2r-first",
            now=p2p.ORIGIN + timedelta(days=4),
        )
        assert result.actions > 0
        posted = store.entity("journal_entry", journal.id)
        assert posted.state == "posted"
        version = posted.version
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            delivery = next(
                d for d in uow.boundary_deliveries()
                if d.contract_name == "accounting.entry_requested"
            )
            receipt = uow.get_boundary_consumption(delivery.delivery_id)
            assert receipt is not None
            assert uow.get_command(receipt.consumer_effect_id) is None
        assert worker.run_trigger(
            trigger_id="replenishment-r2r-second",
            now=p2p.ORIGIN + timedelta(days=4, minutes=1),
        ).actions == 0
        assert store.entity("journal_entry", journal.id).version == version

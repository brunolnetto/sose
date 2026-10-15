"""Owner-qualified causal Commands on the Trading Company ACK boundary.

A worker lease is not an operational organization; an accepted boundary
message must persist a Command bound to the explicitly selected organization.
"""
from datetime import datetime, timezone

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition import trading_company_replenishment as replenishment
from sose.composition.model import BoundaryMessage
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def _message() -> BoundaryMessage:
    return BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready",
        contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="fulfillment-1",
        destination_domain="logistics",
        occurrence_key="dispatch-1",
        correlation_id="customer-order-1",
        causation_id="fulfillment-certified-1",
        produced_at=NOW,
        payload={"shipment_id": "shipment-1"},
    )


@pytest.mark.parametrize("handler_factory", [
    lambda owner: customer._intent_handler(
        intent_name="composition.deliver_shipment",
        entity_type="shipment",
        entity_id="shipment-1",
        owner_id=owner,
    ),
    lambda owner: replenishment._intent_handler(
        name="composition.deliver_shipment",
        entity_type="shipment",
        entity_id="shipment-1",
        owner_id=owner,
    ),
])
def test_accepted_boundary_command_is_owner_bound_and_replay_is_idempotent(handler_factory):
    store = MemoryPersistence()
    message = _message()
    handler = handler_factory("organization-a")

    with store.transaction() as uow:
        effect_id = handler(message, uow)

    persisted = store.command(effect_id)
    assert persisted is not None
    assert persisted.owner_id == "organization-a"
    assert persisted.correlation_id == message.correlation_id
    assert persisted.causation_id == message.message_id

    with store.transaction() as uow:
        assert handler(message, uow) == effect_id
    assert store.command(effect_id) == persisted

    # Replaying the *same* causal message under another owner is a conflict,
    # not permission to steal a durable Command or silently rebind it.
    with pytest.raises(ValueError, match="composition intent identity conflict"):
        with store.transaction() as uow:
            handler_factory("organization-b")(message, uow)
    assert store.command(effect_id) == persisted


@pytest.mark.parametrize("handler_factory", [
    lambda owner: customer._intent_handler(
        intent_name="composition.deliver_shipment",
        entity_type="shipment", entity_id="shipment-1", owner_id=owner,
    ),
    lambda owner: replenishment._intent_handler(
        name="composition.deliver_shipment",
        entity_type="shipment", entity_id="shipment-1", owner_id=owner,
    ),
])
@pytest.mark.parametrize("invalid_owner", [None, "", "  "])
def test_composition_rejects_missing_operational_owner(handler_factory, invalid_owner):
    with pytest.raises(ValueError, match="operational owner_id"):
        handler_factory(invalid_owner)


def test_recovered_registry_persists_correlation_bound_owner_not_worker_lease():
    store = MemoryPersistence()
    message = _message()
    registry = TradingCustomerRecoveryRunner._registry_for(
        message, organization_id="organization-a",
    )
    with store.transaction() as uow:
        effect_id = registry.resolve(message)(message, uow)

    command = store.command(effect_id)
    assert command is not None
    assert command.owner_id == "organization-a"
    assert command.correlation_id == "customer-order-1"
    assert command.causation_id == message.message_id

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, DeliveryStatus
from sose.composition.model import BoundaryMessage
from sose.composition.organization import (
    BoundaryBinding,
    OrganizationDefinition,
    ProcessComponent,
)
from sose.composition.organization_runtime import (
    OrganizationBoundaryConsumerRegistry,
    OrganizationBoundaryService,
)
from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import ProcessEvidence, builtin_process_manifests
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CONTRACT = "warehouse.inventory_reservation_requested"


def _definition() -> OrganizationDefinition:
    return OrganizationDefinition(
        organization_id="organization:runtime-test",
        version=1,
        config_epoch=7,
        components=(
            ProcessComponent(
                component_id="fulfillment",
                domain="warehouse_fulfillment",
                owner_id="owner:fulfillment",
                statechart_types=("FulfillmentOrderChart",),
            ),
            ProcessComponent(
                component_id="warehouse",
                domain="warehouse_management",
                owner_id="owner:warehouse",
                statechart_types=("WarehouseSiteChart",),
            ),
        ),
        boundaries=(
            BoundaryBinding(
                producer_id="fulfillment",
                consumer_id="warehouse",
                contract_name=CONTRACT,
                contract_version=1,
            ),
        ),
    )


def _audited_manifests():
    manifests = builtin_process_manifests()
    original = manifests["warehouse_management"]
    provenance = dict(original.evidence_sources)
    provenance[ProcessEvidence.INGRESS_CONTRACTS] = (
        "src/sose/composition/trading_company_customer.py",
        "tests/integration/sose/composition/test_pc6_two_real_customer_paths.py",
    )
    manifests["warehouse_management"] = replace(
        original,
        evidence=original.evidence | {ProcessEvidence.INGRESS_CONTRACTS},
        ingress_contracts=frozenset({f"{CONTRACT}.v1"}),
        evidence_sources=provenance,
        contract_provenance=(
            (
                "ingress",
                f"{CONTRACT}.v1",
                (
                    "src/sose/composition/trading_company_customer.py",
                    "tests/integration/sose/composition/test_pc6_two_real_customer_paths.py",
                ),
            ),
        ),
    )
    return manifests


def _runtime():
    definition = _definition()
    contracts = BoundaryConsumerRegistry()
    contracts.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name=CONTRACT,
        contract_version=1,
        handler=lambda _message, _uow: "domain-contract-placeholder",
    )
    registry = OrganizationBoundaryConsumerRegistry(
        definition,
        catalog=builtin_catalog(),
        manifests=_audited_manifests(),
        contracts=contracts,
    )
    registry.register(
        producer_component_id="fulfillment",
        consumer_component_id="warehouse",
        contract_name=CONTRACT,
        contract_version=1,
        handler=lambda message, _uow: f"reserved:{message.source_identity}",
    )
    return definition, registry


def _message(definition: OrganizationDefinition, *, organization_id=None, source_owner_id=None):
    return BoundaryMessage.create(
        contract_name=CONTRACT,
        contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="order:42",
        destination_domain="warehouse_management",
        occurrence_key="reservation:1",
        correlation_id="flow:42",
        causation_id="event:fulfillment:42",
        causation_kind="event",
        produced_at=NOW,
        payload={"order_id": "42", "sku": "widget-a", "quantity": 2},
        organization_id=organization_id or definition.organization_id,
        definition_fingerprint=definition.fingerprint(),
        config_epoch=definition.config_epoch,
        source_component_id="fulfillment",
        source_owner_id=source_owner_id or "owner:fulfillment",
        destination_component_id="warehouse",
        destination_owner_id="owner:warehouse",
    )


def test_v1_boundary_message_identity_remains_byte_compatible():
    message = BoundaryMessage.create(
        contract_name=CONTRACT,
        contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="order:42",
        destination_domain="warehouse_management",
        occurrence_key="reservation:1",
        correlation_id="flow:42",
        causation_id="event:fulfillment:42",
        causation_kind="event",
        produced_at=NOW,
        payload={"quantity": 2},
    )
    assert not message.organization_scoped
    assert message.message_id == deterministic_id(
        "boundary-message",
        "warehouse_fulfillment",
        "order:42",
        CONTRACT,
        1,
        "warehouse_management",
        "reservation:1",
    )


def test_organization_boundary_binding_is_all_or_none():
    with pytest.raises(ValueError, match="binding must be complete"):
        BoundaryMessage.create(
            contract_name=CONTRACT,
            contract_version=1,
            source_domain="warehouse_fulfillment",
            source_identity="order:42",
            destination_domain="warehouse_management",
            occurrence_key="reservation:1",
            correlation_id="flow:42",
            causation_id="event:fulfillment:42",
            causation_kind="event",
            produced_at=NOW,
            payload={},
            organization_id="organization:runtime-test",
        )


def test_organization_boundary_message_identity_includes_owner_and_definition():
    definition, _registry = _runtime()
    original = _message(definition)
    different_org = _message(definition, organization_id="organization:other")
    different_owner = _message(definition, source_owner_id="owner:other")

    assert original.organization_scoped
    assert original.message_id != different_org.message_id
    assert original.message_id != different_owner.message_id
    with pytest.raises(ValueError, match="identity contradicts binding"):
        replace(original, destination_owner_id="owner:attacker")


def test_component_router_requires_exact_admitted_edge_and_owner():
    definition, registry = _runtime()
    message = _message(definition)

    assert registry.resolve(message)(message, None) == "reserved:order:42"
    with pytest.raises(PermissionError, match="owner/domain binding mismatch"):
        registry.validate_message(_message(definition, source_owner_id="owner:forged"))
    with pytest.raises(ValueError, match="different organization definition"):
        registry.validate_message(
            _message(definition, organization_id="organization:other")
        )
    with pytest.raises(ValueError, match="route is not admitted"):
        registry.register(
            producer_component_id="warehouse",
            consumer_component_id="fulfillment",
            contract_name=CONTRACT,
            contract_version=1,
            handler=lambda _message, _uow: "must-not-register",
        )


def test_publish_claim_ack_is_scoped_to_exact_component_route_and_source_owner():
    definition, registry = _runtime()
    persistence = MemoryPersistence()
    service = OrganizationBoundaryService(persistence, registry)
    message = _message(definition)
    source_event = DomainEvent(
        event_id="event:fulfillment:42",
        name="fulfillment.reservation_requested",
        entity_type="warehouse_fulfillment_order",
        entity_id="42",
        occurred_at=NOW,
        correlation_id="flow:42",
        owner_id="owner:fulfillment",
    )
    with persistence.transaction() as uow:
        uow.append_event(source_event)

    delivery = service.publish(message)
    assert delivery.message_id == message.message_id
    lease = service.claim(
        message_id=message.message_id,
        owner_id="worker:warehouse",
        now=NOW + timedelta(seconds=1),
        lease_duration=timedelta(seconds=30),
    )
    assert lease is not None
    receipt = service.consume(lease=lease, now=NOW + timedelta(seconds=2))
    assert receipt.message_id == message.message_id
    assert service.delivery(delivery.delivery_id).status is DeliveryStatus.CONSUMED
    assert service.consume(lease=lease, now=NOW + timedelta(seconds=3)) == receipt


def test_publish_rejects_event_from_another_component_owner_without_persisting():
    definition, registry = _runtime()
    persistence = MemoryPersistence()
    service = OrganizationBoundaryService(persistence, registry)
    message = _message(definition)
    foreign_event = DomainEvent(
        event_id="event:foreign:42",
        name="fulfillment.reservation_requested",
        entity_type="warehouse_fulfillment_order",
        entity_id="42",
        occurred_at=NOW,
        correlation_id="flow:42",
        owner_id="owner:somebody-else",
    )
    with persistence.transaction() as uow:
        uow.append_event(foreign_event)
    forged = replace(message, causation_id="event:foreign:42")
    # The changed cause does not change immutable message routing identity;
    # source event ownership is independently checked at publication.
    with pytest.raises(PermissionError, match="different component owner"):
        service.publish(forged)
    with persistence.transaction() as uow:
        assert uow.boundary_messages() == ()


def test_exact_message_claim_does_not_select_another_organizations_delivery():
    definition, registry = _runtime()
    persistence = MemoryPersistence()
    service = OrganizationBoundaryService(persistence, registry)
    message = _message(definition)
    source_event = DomainEvent(
        event_id="event:fulfillment:42",
        name="fulfillment.reservation_requested",
        entity_type="warehouse_fulfillment_order",
        entity_id="42",
        occurred_at=NOW,
        correlation_id="flow:42",
        owner_id="owner:fulfillment",
    )
    with persistence.transaction() as uow:
        uow.append_event(source_event)
    service.publish(message)

    with pytest.raises(KeyError, match="unknown organization boundary message"):
        service.claim(
            message_id="message:another-organization",
            owner_id="worker:warehouse",
            now=NOW + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        )


def test_v1_codec_omits_new_optional_organization_fields():
    import json

    from sose.persistence.codec import dumps, loads

    message = BoundaryMessage.create(
        contract_name=CONTRACT,
        contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="order:42",
        destination_domain="warehouse_management",
        occurrence_key="reservation:1",
        correlation_id="flow:42",
        causation_id=None,
        produced_at=NOW,
        payload={"quantity": 2},
    )
    encoded = dumps(message)
    fields = json.loads(encoded)["fields"]
    assert "causation_kind" not in fields
    for name in (
        "organization_id",
        "definition_fingerprint",
        "config_epoch",
        "source_component_id",
        "source_owner_id",
        "destination_component_id",
        "destination_owner_id",
    ):
        assert name not in fields
    assert loads(encoded) == message


def test_v2_codec_roundtrip_preserves_complete_organization_binding():
    from sose.persistence.codec import dumps, loads

    definition, _registry = _runtime()
    message = _message(definition)
    restored = loads(dumps(message))
    assert restored == message
    assert restored.organization_scoped
    assert restored.definition_fingerprint == definition.fingerprint()

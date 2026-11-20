"""Fail-closed guards of the P496-A organization boundary runtime (#496)."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    UnsupportedBoundaryContractError,
)
from sose.composition.model import BoundaryDelivery, BoundaryLease, BoundaryMessage
from sose.composition.organization_runtime import (
    OrganizationBoundaryConsumerRegistry,
    OrganizationBoundaryService,
    _require_terminating_predecessor,
)
from sose.core.events import DomainEvent
from sose.examples.catalog import builtin_catalog
from sose.persistence.memory import MemoryPersistence

from tests.unit.sose.composition.test_organization_boundary_runtime import (
    CONTRACT,
    NOW,
    _audited_manifests,
    _definition,
    _message,
    _runtime,
)


def _event(**changes) -> DomainEvent:
    values = dict(
        event_id="event:fulfillment:42",
        name="fulfillment.reservation_requested",
        entity_type="warehouse_fulfillment_order",
        entity_id="42",
        occurred_at=NOW,
        correlation_id="flow:42",
        owner_id="owner:fulfillment",
    )
    values.update(changes)
    return DomainEvent(**values)


def _service_with(*events: DomainEvent):
    definition, registry = _runtime()
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        for event in events:
            uow.append_event(event)
    return definition, OrganizationBoundaryService(persistence, registry)


def _unregistered_registry():
    contracts = BoundaryConsumerRegistry()
    contracts.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name=CONTRACT,
        contract_version=1,
        handler=lambda _message, _uow: "domain-contract-placeholder",
    )
    return contracts, OrganizationBoundaryConsumerRegistry(
        _definition(),
        catalog=builtin_catalog(),
        manifests=_audited_manifests(),
        contracts=contracts,
    )


# -- BoundaryMessage v2 binding validation ---------------------------------


@pytest.mark.parametrize("version", [True, 1.0])
def test_message_rejects_non_integer_contract_version(version):
    definition, _registry = _runtime()
    with pytest.raises(
        ValueError, match=r"contract_version must be a positive integer \(not bool/float\)"
    ):
        replace(_message(definition), contract_version=version)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"source_owner_id": " owner:fulfillment"}, "source_owner_id must be a canonical"),
        ({"organization_id": ""}, "organization_id must be a canonical"),
        ({"destination_component_id": 7}, "destination_component_id must be a canonical"),
        ({"config_epoch": -1}, "config_epoch must be a nonnegative integer"),
        ({"config_epoch": True}, "config_epoch must be a nonnegative integer"),
        ({"definition_fingerprint": "ABC"}, "definition_fingerprint must be a lowercase"),
        ({"definition_fingerprint": 5}, "definition_fingerprint must be a lowercase"),
        ({"source_component_id": None}, "binding must be complete"),
    ],
)
def test_message_rejects_malformed_organization_binding(changes, reason):
    definition, _registry = _runtime()
    with pytest.raises(ValueError, match=reason):
        replace(_message(definition), **changes)


# -- OrganizationBoundaryConsumerRegistry -----------------------------------


def test_registry_delegates_audited_domain_contract_queries():
    contracts, registry = _unregistered_registry()
    assert registry.contracts_for("warehouse_management") == contracts.contracts_for(
        "warehouse_management"
    )
    assert registry.contracts_for_source(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
    ) == frozenset({(CONTRACT, 1)})
    assert registry.source_entity_types("fulfillment")
    with pytest.raises(ValueError, match="unknown source component"):
        registry.source_entity_types("nobody")


@pytest.mark.parametrize(
    ("changes", "error", "reason"),
    [
        ({"contract_version": True}, ValueError, "positive integer"),
        ({"contract_version": 0}, ValueError, "positive integer"),
        ({"contract_name": " "}, ValueError, "nonempty identity"),
        ({"handler": "not-callable"}, ValueError, "must be callable"),
    ],
)
def test_registry_rejects_invalid_registration(changes, error, reason):
    _contracts, registry = _unregistered_registry()
    request = dict(
        producer_component_id="fulfillment",
        consumer_component_id="warehouse",
        contract_name=CONTRACT,
        contract_version=1,
        handler=lambda _message, _uow: "ok",
    )
    request.update(changes)
    with pytest.raises(error, match=reason):
        registry.register(**request)


def test_registry_rejects_duplicate_handler_registration():
    _definition_value, registry = _runtime()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            producer_component_id="fulfillment",
            consumer_component_id="warehouse",
            contract_name=CONTRACT,
            contract_version=1,
            handler=lambda _message, _uow: "again",
        )


def test_registry_rejects_non_message_unscoped_and_unknown_components():
    definition, registry = _runtime()
    with pytest.raises(TypeError, match="requires BoundaryMessage"):
        registry.validate_message(object())
    unscoped = BoundaryMessage.create(
        contract_name=CONTRACT,
        contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="order:42",
        destination_domain="warehouse_management",
        occurrence_key="reservation:1",
        correlation_id="flow:42",
        causation_id=None,
        produced_at=NOW,
        payload={},
    )
    with pytest.raises(ValueError, match="requires a v2 scoped message"):
        registry.validate_message(unscoped)
    unknown = _rebound(_message(definition), source_component_id="ghost")
    with pytest.raises(ValueError, match="unknown component"):
        registry.validate_message(unknown)


def test_registry_rejects_unadmitted_edge_and_missing_handler():
    definition, registry = _runtime()
    other_version = _rebound(_message(definition), contract_version=2)
    with pytest.raises(ValueError, match="admitted component edge"):
        registry.validate_message(other_version)
    _contracts, bare = _unregistered_registry()
    with pytest.raises(UnsupportedBoundaryContractError, match="no registered handler"):
        bare.validate_message(_message(definition))


def _rebound(message: BoundaryMessage, **changes) -> BoundaryMessage:
    values = {
        name: getattr(message, name)
        for name in (
            "contract_name", "contract_version", "source_domain", "source_identity",
            "destination_domain", "occurrence_key", "correlation_id", "causation_id",
            "produced_at", "causation_kind", "organization_id", "definition_fingerprint",
            "config_epoch", "source_component_id", "source_owner_id",
            "destination_component_id", "destination_owner_id",
        )
    }
    values.update(changes)
    return BoundaryMessage.create(payload=message.payload(), **values)


# -- OrganizationBoundaryService.publish -------------------------------------


def test_publish_requires_typed_causation():
    definition, service = _service_with(_event())
    untyped = _rebound(_message(definition), causation_kind=None)
    with pytest.raises(ValueError, match="typed event or boundary causation"):
        service.publish(untyped)


@pytest.mark.parametrize(
    ("event", "error", "reason"),
    [
        (None, ValueError, "source event is missing"),
        (_event(entity_type="foreign_entity"), PermissionError, "entity type is not owned"),
        (_event(correlation_id="flow:other"), ValueError, "correlation contradicts"),
        (
            _event(occurred_at=NOW + timedelta(seconds=1)),
            ValueError,
            "precedes its source event",
        ),
    ],
)
def test_publish_rejects_unproven_source_event(event, error, reason):
    events = () if event is None else (event,)
    definition, service = _service_with(*events)
    with pytest.raises(error, match=reason):
        service.publish(_message(definition))
    with service.persistence.transaction() as uow:
        assert uow.boundary_messages() == ()


def test_publish_rejects_missing_or_non_terminating_boundary_predecessor():
    definition, service = _service_with(_event())
    parent = _message(definition)
    child = _rebound(
        parent,
        occurrence_key="reservation:2",
        causation_id=parent.message_id,
        causation_kind="boundary",
    )
    with pytest.raises(ValueError, match="predecessor is missing"):
        service.publish(child)
    service.publish(parent)
    # The parent terminates at the warehouse, not at the child's source.
    with pytest.raises(PermissionError, match="does not terminate"):
        service.publish(child)


class _AcceptingRegistry:
    def validate_message(self, _message):
        return None


@pytest.mark.parametrize(
    ("parent_changes", "reason"),
    [
        ({"correlation_id": "flow:other"}, "predecessor correlation mismatch"),
        ({"produced_at": NOW + timedelta(seconds=1)}, "precedes its boundary predecessor"),
    ],
)
def test_terminating_predecessor_requires_correlation_and_order(parent_changes, reason):
    child = SimpleNamespace(
        causation_id="parent",
        source_component_id="warehouse",
        source_owner_id="owner:warehouse",
        correlation_id="flow:42",
        produced_at=NOW,
    )
    parent = SimpleNamespace(
        destination_component_id="warehouse",
        destination_owner_id="owner:warehouse",
        correlation_id="flow:42",
        produced_at=NOW,
    )
    for name, value in parent_changes.items():
        setattr(parent, name, value)
    uow = SimpleNamespace(get_boundary_message=lambda _message_id: parent)
    with pytest.raises(ValueError, match=reason):
        _require_terminating_predecessor(uow, child, _AcceptingRegistry())
    # The well-ordered, same-correlation predecessor is accepted.
    parent.correlation_id, parent.produced_at = "flow:42", NOW
    _require_terminating_predecessor(uow, child, _AcceptingRegistry())


# -- claim / delivery / consume ---------------------------------------------


def test_claim_rejects_empty_message_identity():
    _definition_value, service = _service_with()
    with pytest.raises(ValueError, match="nonempty exact identity"):
        service.claim(
            message_id="",
            owner_id="worker:warehouse",
            now=NOW,
            lease_duration=timedelta(seconds=30),
        )


class _StubUow:
    def __init__(self, delivery=None, message=None):
        self._delivery = delivery
        self._message = message

    def get_boundary_delivery(self, _delivery_id):
        return self._delivery

    def get_boundary_message(self, _message_id):
        return self._message


class _StubPersistence:
    def __init__(self, uow):
        self._uow = uow

    @contextmanager
    def transaction(self):
        yield self._uow


def test_delivery_read_returns_none_and_rejects_orphaned_or_contradicting_rows():
    definition, registry = _runtime()
    message = _message(definition)
    pending = BoundaryDelivery.pending(message)

    absent = OrganizationBoundaryService(_StubPersistence(_StubUow()), registry)
    assert absent.delivery("missing") is None

    orphan = OrganizationBoundaryService(
        _StubPersistence(_StubUow(delivery=pending)), registry,
    )
    with pytest.raises(RuntimeError, match="has no publication"):
        orphan.delivery(pending.delivery_id)

    contradicting = OrganizationBoundaryService(
        _StubPersistence(
            _StubUow(delivery=replace(pending, delivery_id="forged"), message=message)
        ),
        registry,
    )
    with pytest.raises(RuntimeError, match="contradicts publication"):
        contradicting.delivery("forged")


def test_consume_rejects_lease_for_unknown_message():
    _definition_value, service = _service_with()
    lease = BoundaryLease(
        delivery_id="delivery:x",
        message_id="message:unknown",
        owner_id="worker:warehouse",
        epoch=1,
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(KeyError, match="unknown organization boundary message"):
        service.consume(lease=lease, now=NOW)

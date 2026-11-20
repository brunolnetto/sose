"""P496-A organization boundary routing through real persistence (#496).

Runs on memory, SQLite and PostgreSQL. Covers a component boundary ->
boundary causal chain end to end, and that generic (domain-level) publish,
claim and dispatch cannot bypass the organization router's owner checks.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    BoundaryService,
    DeliveryStatus,
    UnsupportedBoundaryContractError,
)
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
from sose.domain.config import DomainCatalog
from sose.domain.registry import EntityType
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import ProcessEvidence, builtin_process_manifests
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

from tests.unit.sose.composition.test_organization_boundary_runtime import NOW

REQUEST = "warehouse.inventory_reservation_requested"
REPLY = "warehouse.dispatch_ready"
_SOURCES = (
    "src/sose/composition/trading_company_customer.py",
    "tests/integration/sose/composition/test_pc6_two_real_customer_paths.py",
)
WF_OWNER = "owner:fulfillment"
WM_OWNER = "owner:warehouse"
LG_OWNER = "owner:logistics"
_DOMAINS = {
    "fulfillment": "warehouse_fulfillment",
    "warehouse": "warehouse_management",
    "logistics": "logistics",
}
_OWNERS = {"fulfillment": WF_OWNER, "warehouse": WM_OWNER, "logistics": LG_OWNER}


@pytest.fixture(params=("memory", "sqlite", "postgres"))
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryPersistence()
        return
    if request.param == "sqlite":
        persistence = SQLitePersistence(str(tmp_path / "org-boundary.db"))
    else:
        dsn = os.environ.get("SOSE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("SOSE_TEST_POSTGRES_DSN is required")
        from sose.persistence.postgres import PostgresPersistence

        persistence = PostgresPersistence(dsn, namespace="p496_" + uuid.uuid4().hex[:12])
    try:
        yield persistence
    finally:
        persistence.close()


def _definition() -> OrganizationDefinition:
    return OrganizationDefinition(
        organization_id="organization:backend-test",
        version=1,
        config_epoch=3,
        components=(
            ProcessComponent(
                component_id="fulfillment",
                domain="warehouse_fulfillment",
                owner_id=WF_OWNER,
                statechart_types=("FulfillmentOrderChart",),
            ),
            ProcessComponent(
                component_id="warehouse",
                domain="warehouse_management",
                owner_id=WM_OWNER,
                statechart_types=("WarehouseSiteChart",),
            ),
            ProcessComponent(
                component_id="logistics",
                domain="logistics",
                owner_id=LG_OWNER,
                statechart_types=("ShipmentChart",),
            ),
        ),
        boundaries=(
            BoundaryBinding(
                producer_id="fulfillment", consumer_id="warehouse",
                contract_name=REQUEST, contract_version=1,
            ),
            BoundaryBinding(
                producer_id="warehouse", consumer_id="logistics",
                contract_name=REPLY, contract_version=1,
            ),
        ),
    )


def _attest(manifest, *, ingress=(), egress=()):
    provenance = dict(manifest.evidence_sources)
    evidence = set(manifest.evidence)
    declared = []
    if ingress:
        provenance[ProcessEvidence.INGRESS_CONTRACTS] = _SOURCES
        evidence.add(ProcessEvidence.INGRESS_CONTRACTS)
        declared += [("ingress", f"{name}.v1", _SOURCES) for name in ingress]
    if egress:
        provenance[ProcessEvidence.EGRESS_CONTRACTS] = _SOURCES
        evidence.add(ProcessEvidence.EGRESS_CONTRACTS)
        declared += [("egress", f"{name}.v1", _SOURCES) for name in egress]
    return replace(
        manifest,
        evidence=frozenset(evidence),
        ingress_contracts=frozenset(f"{name}.v1" for name in ingress),
        egress_contracts=frozenset(f"{name}.v1" for name in egress),
        evidence_sources=provenance,
        contract_provenance=tuple(declared),
    )


def _manifests():
    manifests = builtin_process_manifests()
    manifests["warehouse_management"] = _attest(
        manifests["warehouse_management"], ingress=(REQUEST,), egress=(REPLY,),
    )
    manifests["logistics"] = _attest(manifests["logistics"], ingress=(REPLY,))
    return manifests


def ShipmentChart():  # noqa: N802 - named like the StateChart factory it stands for
    raise AssertionError("admission must never build a StateChart")


def _catalog() -> DomainCatalog:
    """Builtin WF/WM plus a logistics consumer with one declared StateChart.

    The builtin logistics definition registers no StateChart yet, and only
    WF/WM do; a third, distinct-domain component is needed because admission
    rejects cyclic and same-domain boundary graphs.
    """
    builtin = builtin_catalog()
    catalog = DomainCatalog()
    catalog.register(builtin.get("warehouse_fulfillment"))
    catalog.register(builtin.get("warehouse_management"))
    catalog.register(replace(
        builtin.get("logistics"),
        registered_entity_types=(EntityType("shipment", ShipmentChart),),
    ))
    return catalog


def _domain_contracts() -> BoundaryConsumerRegistry:
    contracts = BoundaryConsumerRegistry()
    for source, destination, name in (
        ("warehouse_fulfillment", "warehouse_management", REQUEST),
        ("warehouse_management", "logistics", REPLY),
    ):
        contracts.register(
            source_domain=source, destination_domain=destination,
            contract_name=name, contract_version=1,
            handler=lambda _message, _uow: "DOMAIN-HANDLER-MUST-NOT-RUN",
        )
    return contracts


def _router(definition: OrganizationDefinition) -> OrganizationBoundaryConsumerRegistry:
    router = OrganizationBoundaryConsumerRegistry(
        definition,
        catalog=_catalog(),
        manifests=_manifests(),
        contracts=_domain_contracts(),
    )
    for producer, consumer, name in (
        ("fulfillment", "warehouse", REQUEST),
        ("warehouse", "logistics", REPLY),
    ):
        router.register(
            producer_component_id=producer, consumer_component_id=consumer,
            contract_name=name, contract_version=1,
            handler=lambda message, _uow, tag=name: f"{tag}:{message.occurrence_key}",
        )
    return router


def _scoped(definition, *, contract, source, destination, cause, kind, at, key):
    return BoundaryMessage.create(
        contract_name=contract, contract_version=1,
        source_domain=_DOMAINS[source], source_identity="order:42",
        destination_domain=_DOMAINS[destination], occurrence_key=key,
        correlation_id="flow:42", causation_id=cause, causation_kind=kind,
        produced_at=at, payload={"order_id": "42", "step": key},
        organization_id=definition.organization_id,
        definition_fingerprint=definition.fingerprint(),
        config_epoch=definition.config_epoch,
        source_component_id=source, source_owner_id=_OWNERS[source],
        destination_component_id=destination, destination_owner_id=_OWNERS[destination],
    )


def _seed_source_event(store, owner_id: str = WF_OWNER) -> None:
    with store.transaction() as uow:
        uow.append_event(DomainEvent(
            event_id="event:fulfillment:42",
            name="fulfillment.reservation_requested",
            entity_type="warehouse_fulfillment_order",
            entity_id="42",
            occurred_at=NOW,
            correlation_id="flow:42",
            owner_id=owner_id,
        ))


def _request(definition) -> BoundaryMessage:
    return _scoped(
        definition, contract=REQUEST, source="fulfillment", destination="warehouse",
        cause="event:fulfillment:42", kind="event", at=NOW, key="request",
    )


def _claim_and_consume(service, message, at):
    lease = service.claim(
        message_id=message.message_id, owner_id="worker:org",
        now=at, lease_duration=timedelta(seconds=30),
    )
    assert lease is not None
    return service.consume(lease=lease, now=at + timedelta(seconds=1))


def _stored_messages(store) -> tuple[BoundaryMessage, ...]:
    with store.transaction() as uow:
        return uow.boundary_messages()


def test_component_boundary_chain_round_trips_through_persistence(store):
    definition = _definition()
    service = OrganizationBoundaryService(store, _router(definition))
    _seed_source_event(store)
    request = _request(definition)
    reply = _scoped(
        definition, contract=REPLY, source="warehouse", destination="logistics",
        cause=request.message_id, kind="boundary", at=NOW + timedelta(seconds=5),
        key="reply",
    )

    service.publish(request)
    service.publish(reply)
    # The reply is causally blocked until its predecessor is consumed.
    assert service.claim(
        message_id=reply.message_id, owner_id="worker:org",
        now=NOW + timedelta(seconds=6), lease_duration=timedelta(seconds=30),
    ) is None
    first = _claim_and_consume(service, request, NOW + timedelta(seconds=6))
    second = _claim_and_consume(service, reply, NOW + timedelta(seconds=8))

    assert first.consumer_effect_id == f"{REQUEST}:request"
    assert second.consumer_effect_id == f"{REPLY}:reply"
    assert set(_stored_messages(store)) == {request, reply}
    assert service.publish(reply).status is DeliveryStatus.CONSUMED


def test_reply_must_start_where_its_predecessor_terminated(store):
    definition = _definition()
    service = OrganizationBoundaryService(store, _router(definition))
    _seed_source_event(store)
    request = _request(definition)
    service.publish(request)
    # A second request "caused by" the first starts at fulfillment, but the
    # first terminated at the warehouse component/owner.
    forged = _scoped(
        definition, contract=REQUEST, source="fulfillment", destination="warehouse",
        cause=request.message_id, kind="boundary", at=NOW + timedelta(seconds=1),
        key="forged",
    )
    with pytest.raises(PermissionError, match="does not terminate"):
        service.publish(forged)
    assert _stored_messages(store) == (request,)


def test_generic_publish_refuses_organization_scoped_message(store):
    definition = _definition()
    _seed_source_event(store, owner_id=WM_OWNER)
    with pytest.raises(PermissionError, match="organization boundary facade"):
        BoundaryService(store).publish(_request(definition))
    assert _stored_messages(store) == ()


def test_facade_publish_refuses_foreign_owned_source_event(store):
    definition = _definition()
    service = OrganizationBoundaryService(store, _router(definition))
    _seed_source_event(store, owner_id=WM_OWNER)
    with pytest.raises(PermissionError, match="different component owner"):
        service.publish(_request(definition))
    assert _stored_messages(store) == ()


def test_generic_claim_and_dispatch_cannot_consume_organization_message(store):
    definition = _definition()
    service = OrganizationBoundaryService(store, _router(definition))
    _seed_source_event(store)
    request = _request(definition)
    delivery = service.publish(request)
    generic = BoundaryService(store)

    for selection in (
        {"destination_domain": "warehouse_management"},
        {"message_id": request.message_id},
        {},
    ):
        assert generic.claim_next(
            owner_id="worker:domain", now=NOW + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30), **selection,
        ) is None

    lease = service.claim(
        message_id=request.message_id, owner_id="worker:org",
        now=NOW + timedelta(seconds=1), lease_duration=timedelta(seconds=30),
    )
    with pytest.raises(UnsupportedBoundaryContractError, match="organization router"):
        generic.consume(
            lease=lease, registry=_domain_contracts(), now=NOW + timedelta(seconds=2),
        )
    assert service.delivery(delivery.delivery_id).status is DeliveryStatus.CLAIMED
    receipt = service.consume(lease=lease, now=NOW + timedelta(seconds=2))
    assert receipt.consumer_effect_id == f"{REQUEST}:request"

"""W3c: observational PostgreSQL snapshots never require a writer epoch."""
from dataclasses import replace
from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.boundary_observation import inspect_selected_boundary_protocol
from sose.composition.model import BoundaryMessage, DeliveryStatus
from sose.composition.organization import (
    BoundaryBinding, OrganizationDefinition, ProcessComponent,
)
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import (
    ProcessEvidence, builtin_process_manifests,
)
from sose.persistence.postgres import PostgresPersistence, StaleWriterError

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")
CONTRACT = "warehouse.inventory_reservation_requested"
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def test_pg_selected_boundary_observer_uses_coherent_read_only_snapshot_after_fencing():
    assert DSN
    ns = "w3c_read_" + uuid4().hex[:16]
    definition = OrganizationDefinition(
        organization_id="org:bounded-pg-read", version=1,
        components=(
            ProcessComponent("fulfillment", "warehouse_fulfillment", "owner:wf",
                             ("FulfillmentOrderChart",)),
            ProcessComponent("warehouse", "warehouse_management", "owner:wm",
                             ("WarehouseSiteChart",)),
        ),
        boundaries=(BoundaryBinding(
            "fulfillment", "warehouse", CONTRACT, 1,
        ),),
    )
    manifests = builtin_process_manifests()
    original = manifests["warehouse_management"]
    evidence_file = "tests/e2e/sose/persistence/test_postgres_boundary_protocol_observation.py"
    manifests["warehouse_management"] = replace(
        original,
        evidence=original.evidence | {ProcessEvidence.INGRESS_CONTRACTS},
        ingress_contracts=frozenset({CONTRACT + ".v1"}),
        evidence_sources={
            **original.evidence_sources,
            ProcessEvidence.INGRESS_CONTRACTS: (evidence_file,),
        },
        contract_provenance=((
            "ingress", CONTRACT + ".v1", (evidence_file,),
        ),),
    )
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name=CONTRACT, contract_version=1,
        handler=lambda _message, _uow: "effect:owned",
    )
    source = BoundaryMessage.create(
        contract_name=CONTRACT, contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="order:durable",
        destination_domain="warehouse_management",
        occurrence_key="fenced-observer",
        correlation_id="flow:durable", causation_id=None,
        produced_at=NOW, payload={"units": 2},
    )

    with PostgresPersistence(DSN, namespace=ns) as writer:
        delivery = BoundaryService(writer).publish(source)
        recorded_count = writer.persisted_record_count()
        lease = writer.claim_writer("writer:epoch", expected_epoch=writer.writer_epoch())
        assert lease.epoch > 0
        with PostgresPersistence(DSN, namespace=ns) as reader:
            # Regular transactions must remain fenced; observational reads
            # cannot borrow or forge an operational writer epoch.
            with pytest.raises(StaleWriterError, match="owner_epoch is required"):
                with reader.transaction():
                    pass
            snapshots = [reader.boundary_protocol_snapshot() for _ in range(2)]
            assert snapshots[0] == snapshots[1]
            result = inspect_selected_boundary_protocol(
                definition, reader,
                message_ids=(source.message_id,),
                catalog=builtin_catalog(),
                manifests=manifests, consumers=consumers,
            )
            assert len(result.records) == 1
            assert result.records[0].status is DeliveryStatus.PENDING
            assert result.records[0].message_id == source.message_id
            assert result.records[0].delivery_id == delivery.delivery_id
            assert reader.writer_epoch() == lease.epoch
        assert writer.persisted_record_count() == recorded_count
        assert writer.writer_epoch() == lease.epoch

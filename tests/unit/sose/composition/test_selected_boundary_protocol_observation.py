"""#496 W3c bounded protocol observation with durable Memory/SQLite histories.

The isolated fixture explicitly extends one WM ingress manifest. This is a
TEST fixture, not a promotion of the builtin WM manifest or runtime admission.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.model import BoundaryMessage, BoundaryDelivery, DeliveryStatus
from sose.composition.boundary_observation import inspect_selected_boundary_protocol
from sose.composition.organization import BoundaryBinding, OrganizationDefinition, ProcessComponent
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import ProcessEvidence, builtin_process_manifests
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
CONTRACT = "warehouse.inventory_reservation_requested"


def configuration():
    definition = OrganizationDefinition(
        organization_id="organization:receipt-observation", version=1,
        components=(
            ProcessComponent("fulfillment", "warehouse_fulfillment", "owner:wf",
                             ("FulfillmentOrderChart",)),
            ProcessComponent("warehouse", "warehouse_management", "owner:wm",
                             ("WarehouseSiteChart",)),
        ),
        boundaries=(BoundaryBinding("fulfillment", "warehouse", CONTRACT, 1),),
    )
    # Explicit hypothetical ingress evidence exclusively in this test.
    manifests = builtin_process_manifests()
    original = manifests["warehouse_management"]
    evidence_path = "tests/unit/sose/composition/test_selected_boundary_protocol_observation.py"
    manifests["warehouse_management"] = replace(
        original,
        evidence=original.evidence | {ProcessEvidence.INGRESS_CONTRACTS},
        ingress_contracts=frozenset({CONTRACT + ".v1"}),
        evidence_sources={
            **original.evidence_sources,
            ProcessEvidence.INGRESS_CONTRACTS: (evidence_path,),
        },
        contract_provenance=(("ingress", CONTRACT + ".v1", (evidence_path,)),),
    )
    consumers = BoundaryConsumerRegistry()
    consumers.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name=CONTRACT, contract_version=1,
        handler=lambda _message, _uow: "opaque:effect:id",
    )
    return definition, manifests, consumers


def message(*, occurrence="first", destination="warehouse_management",
            contract=CONTRACT):
    return BoundaryMessage.create(
        contract_name=contract, contract_version=1,
        source_domain="warehouse_fulfillment",
        source_identity="order:unattested-owner",
        destination_domain=destination, occurrence_key=occurrence,
        correlation_id="flow:independent", causation_id=None,
        produced_at=NOW, payload={"units": 2},
    )


def observe(spec, store, manifests, consumers, *ids):
    return inspect_selected_boundary_protocol(
        spec, store, message_ids=tuple(ids),
        catalog=builtin_catalog(), manifests=manifests, consumers=consumers,
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_pending_claimed_consumed_protocol_and_restart_are_read_only(adapter, tmp_path):
    path = tmp_path / "receipt-observation.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    spec, manifests, consumers = configuration()
    first, second, unrelated = (
        message(occurrence="a"), message(occurrence="b"),
        message(occurrence="outside", contract="unsupported.contract"),
    )
    try:
        service = BoundaryService(store)
        for m in (first, second, unrelated):
            service.publish(m)
        pending = observe(spec, store, manifests, consumers, first.message_id, second.message_id)
        assert len(pending.records) == 2
        assert all(item.status is DeliveryStatus.PENDING for item in pending.records)
        assert all(item.consumption_id is None for item in pending.records)
        lease = service.claim_next(
            owner_id="worker:wm", now=NOW,
            lease_duration=timedelta(minutes=1),
            message_id=first.message_id,
        )
        assert lease is not None
        claimed = observe(spec, store, manifests, consumers, first.message_id)
        assert claimed.records[0].status is DeliveryStatus.CLAIMED
        assert (claimed.records[0].claim_epoch, claimed.records[0].claim_worker_id) == (1, "worker:wm")
        acknowledgement = service.consume(lease=lease, registry=consumers, now=NOW)
        before = tuple(store.events())
        consumed = observe(spec, store, manifests, consumers, first.message_id, second.message_id)
        by_id = {record.message_id: record for record in consumed.records}
        assert by_id[first.message_id].status is DeliveryStatus.CONSUMED
        assert by_id[first.message_id].consumption_id == acknowledgement.consumption_id
        assert by_id[first.message_id].consumer_effect_id == "opaque:effect:id"
        assert by_id[second.message_id].status is DeliveryStatus.PENDING
        assert consumed == observe(spec, store, manifests, consumers, first.message_id, second.message_id)
        assert tuple(store.events()) == before
        assert consumed.definition_fingerprint == spec.fingerprint()
        assert by_id[first.message_id].declared_producer_component_id == "fulfillment"
        assert by_id[first.message_id].declared_consumer_component_id == "warehouse"
        # No inference from this opaque effect ID to a BusinessEffectApplied.
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert observe(spec, recovered, manifests, consumers, first.message_id, second.message_id) == consumed


@pytest.mark.parametrize("invalid", ("missing", "wrong_contract", "wrong_destination"))
def test_selected_untrusted_or_unadmitted_message_fails_closed(invalid):
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    candidate = message(
        contract="unadmitted.contract" if invalid == "wrong_contract" else CONTRACT,
        destination="order_to_cash" if invalid == "wrong_destination" else "warehouse_management",
    )
    if invalid != "missing":
        BoundaryService(store).publish(candidate)
    with pytest.raises(ValueError, match="missing|no exact admitted edge"):
        observe(spec, store, manifests, consumers, candidate.message_id)


@pytest.mark.parametrize("bad_ids", [
    ("same", "same"), ["not-tuple"], ("",), (" white ",), (7,),
])
def test_selection_requires_unique_canonical_message_ids(bad_ids):
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    with pytest.raises(ValueError, match="unique canonical strings"):
        inspect_selected_boundary_protocol(
            spec, store, message_ids=bad_ids,
            catalog=builtin_catalog(), manifests=manifests, consumers=consumers,
        )


@pytest.mark.parametrize("violation", ("missing_delivery", "wrong_destination",
                                       "consumed_without_ack", "orphan_ack"))
def test_protocol_integrity_rejects_corrupt_durable_ledger(violation):
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    source = message()
    service = BoundaryService(store)
    service.publish(source)
    original = service.delivery(BoundaryDelivery.pending(source).delivery_id)
    with store.transaction() as uow:
        if violation == "missing_delivery":
            # Do not invent a new public delete API. Supply an adversarial UoW
            # snapshot below, simulating storage corruption/recovery mismatch.
            pass
        elif violation == "wrong_destination":
            uow.save_boundary_delivery(replace(original, destination_domain="forged"))
        elif violation == "consumed_without_ack":
            uow.save_boundary_delivery(replace(
                original, status=DeliveryStatus.CONSUMED,
                consumed_at=NOW, consumer_effect_id="opaque:forged",
            ))
    if violation in ("missing_delivery", "orphan_ack"):
        class AdversarialPersistence:
            def transaction(self):
                class Tx:
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return False
                    def boundary_messages(self):
                        return (source,)
                    def boundary_deliveries(self):
                        return () if violation == "missing_delivery" else (original,)
                    def boundary_consumptions(self):
                        if violation == "orphan_ack":
                            from sose.composition.model import BoundaryConsumption
                            return (BoundaryConsumption.create(
                                delivery=original, consumer_effect_id="effect", consumed_at=NOW,
                            ),)
                        return ()
                return Tx()
        store = AdversarialPersistence()
    with pytest.raises(ValueError, match="lacks durable delivery|contradicts|lacks durable ACK|unexpectedly has ACK"):
        observe(spec, store, manifests, consumers, source.message_id)


def test_invalid_admission_does_not_read_protocol_tables():
    spec, manifests, consumers = configuration()
    # The real unmodified builtin WM ingress is not audited.
    class NeverRead:
        def transaction(self):
            raise AssertionError("unadmitted definition read a durable protocol table")
    with pytest.raises(ValueError, match="ingress manifest"):
        inspect_selected_boundary_protocol(
            spec, NeverRead(), message_ids=(),
            catalog=builtin_catalog(), manifests=builtin_process_manifests(), consumers=consumers,
        )


@pytest.mark.parametrize("corruption", ("extra_wrong_delivery_id", "orphan_ack_id"))
def test_unpaired_extra_protocol_descendant_cannot_be_silently_ignored(corruption):
    """Every descendant naming a selected publication is validated, not just a happy pair."""
    from sose.composition.model import BoundaryConsumption

    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    source = message()
    service = BoundaryService(store)
    service.publish(source)
    delivery = BoundaryDelivery.pending(source)
    with store.transaction() as uow:
        if corruption == "extra_wrong_delivery_id":
            uow.save_boundary_delivery(replace(delivery, delivery_id="forged:delivery"))
        else:
            # Receipt claims the selected publication without the selected
            # delivery identity. A pending real delivery cannot hide it.
            uow.save_boundary_consumption(replace(
                BoundaryConsumption.create(
                    delivery=delivery, consumer_effect_id="effect:forged", consumed_at=NOW,
                ),
                delivery_id="forged:delivery",
            ))
    with pytest.raises(ValueError, match="delivery identity contradicts|ACK identity references"):
        observe(spec, store, manifests, consumers, source.message_id)

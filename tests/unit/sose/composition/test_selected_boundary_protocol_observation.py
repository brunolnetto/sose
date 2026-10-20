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
                attempts=1, claim_epoch=1, claim_owner_id="worker:wm",
                claimed_at=NOW, lease_expires_at=NOW + timedelta(minutes=5),
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


@pytest.mark.parametrize("corruption", (
    "no_original_claim", "missing_claim_owner", "counter_drift",
    "ack_before_claim", "ack_after_expiry",
))
def test_matching_ack_cannot_hide_impossible_original_claim(corruption):
    """A forged ACK+delivery pair cannot stand in for a real claim history."""
    from sose.composition.model import BoundaryConsumption
    spec, manifests, consumers = configuration()
    store = MemoryPersistence()
    message_record = message()
    BoundaryService(store).publish(message_record)
    delivery_id = BoundaryDelivery.pending(message_record).delivery_id
    with store.transaction() as uow:
        pending = uow.get_boundary_delivery(delivery_id)
        claimed = replace(
            pending, status=DeliveryStatus.CLAIMED,
            attempts=1, claim_epoch=1, claim_owner_id="worker:wm",
            claimed_at=NOW, lease_expires_at=NOW + timedelta(minutes=5),
        )
        invalid = replace(
            claimed, status=DeliveryStatus.CONSUMED,
            consumed_at=NOW + timedelta(minutes=1),
            consumer_effect_id="opaque:effect",
        )
        if corruption == "no_original_claim":
            invalid = replace(invalid, attempts=0, claim_epoch=0,
                              claim_owner_id=None, claimed_at=None,
                              lease_expires_at=None)
        elif corruption == "missing_claim_owner":
            invalid = replace(invalid, claim_owner_id=None)
        elif corruption == "counter_drift":
            invalid = replace(invalid, claim_epoch=2)
        elif corruption == "ack_before_claim":
            invalid = replace(invalid, consumed_at=NOW - timedelta(seconds=1))
        else:
            invalid = replace(invalid, consumed_at=NOW + timedelta(minutes=6))
        uow.save_boundary_delivery(invalid)
        uow.save_boundary_consumption(BoundaryConsumption.create(
            delivery=invalid, consumer_effect_id="opaque:effect",
            consumed_at=invalid.consumed_at,
        ))
    before = tuple(store.events())
    with pytest.raises(ValueError, match="original claim provenance|claim time fence"):
        observe(spec, store, manifests, consumers, message_record.message_id)
    assert tuple(store.events()) == before



def test_immutable_ack_identity_selection_detects_foreign_delivery_alias():
    """A forged delivery/message ID cannot hide behind the selected ACK ID."""
    from sose.composition.model import BoundaryConsumption

    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    source = message()
    service = BoundaryService(store)
    delivery = service.publish(source)
    receipt = BoundaryConsumption.create(
        delivery=delivery, consumer_effect_id="opaque:effect", consumed_at=NOW,
    )
    # The identity is still the authentic deterministic ACK identifier, but
    # two other fields are forged. A filter by message/delivery alone misses it.
    corrupted = replace(
        receipt, message_id="foreign:message", delivery_id="foreign:delivery",
    )
    with store.transaction() as uow:
        uow.save_boundary_consumption(corrupted)
    with pytest.raises(ValueError, match="ACK identity references foreign delivery"):
        observe(spec, store, manifests, consumers, source.message_id)
    assert service.delivery(delivery.delivery_id) == delivery


def test_selected_message_id_must_match_immutable_publication_fields():
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    original = message()
    forged = replace(original, message_id="boundary:forged:identity")
    with store.transaction() as uow:
        uow.save_boundary_message(forged)
        uow.save_boundary_delivery(BoundaryDelivery.pending(forged))
    with pytest.raises(ValueError, match="forged deterministic identity"):
        observe(spec, store, manifests, consumers, forged.message_id)


@pytest.mark.parametrize("field", ("attempts", "claim_epoch"))
def test_pending_delivery_cannot_be_replayed_with_prior_claim_history(field):
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    source = message()
    delivery = BoundaryService(store).publish(source)
    with store.transaction() as uow:
        uow.save_boundary_delivery(replace(delivery, **{field: 1}))
    with pytest.raises(ValueError, match="pending selected delivery carries impossible claim history"):
        observe(spec, store, manifests, consumers, source.message_id)


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_read_model_accepts_independent_wall_lease_and_future_logical_source(
    adapter, tmp_path,
):
    """The read model must not invent a shared logical/wall-clock authority."""
    path = tmp_path / "independent-clock.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    spec, manifests, consumers = configuration()
    source = message()
    try:
        service = BoundaryService(store)
        delivery = service.publish(source)
        lease_clock = NOW - timedelta(minutes=10)
        lease = service.claim_next(
            owner_id="worker:wm", message_id=source.message_id,
            now=lease_clock, lease_duration=timedelta(minutes=5),
        )
        assert lease is not None
        observed = observe(spec, store, manifests, consumers, source.message_id)
        assert observed.records[0].status is DeliveryStatus.CLAIMED
        ack = service.consume(lease=lease, registry=consumers, now=lease_clock)
        after = observe(spec, store, manifests, consumers, source.message_id)
        assert after.records[0].consumption_id == ack.consumption_id
        assert after.records[0].status is DeliveryStatus.CONSUMED
        assert service.delivery(delivery.delivery_id).consumed_at == lease_clock
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert observe(spec, recovered, manifests, consumers, source.message_id) == after



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_selected_observer_rejects_unknown_delivery_status(adapter, tmp_path):
    """An impossible status never becomes valid read-only protocol evidence."""
    path = tmp_path / "invalid-selected-delivery-status.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    spec, manifests, consumers = configuration()
    source = message()
    try:
        delivery = BoundaryService(store).publish(source)
        with store.transaction() as uow:
            uow.save_boundary_delivery(replace(delivery, status="forged"))
        before = tuple(store.events())
        with pytest.raises(ValueError, match="selected delivery has invalid status"):
            observe(spec, store, manifests, consumers, source.message_id)
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            with pytest.raises(ValueError, match="selected delivery has invalid status"):
                observe(spec, reopened, manifests, consumers, source.message_id)


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_boundary_writer_rejects_ack_before_same_lease_claim_clock(adapter, tmp_path):
    """The service itself must never write an ACK contradicted by its lease."""
    from sose.composition.boundary import StaleBoundaryClaimError

    path = tmp_path / "backwards-lease-clock.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    spec, manifests, consumers = configuration()
    source = message()
    try:
        service = BoundaryService(store)
        delivery = service.publish(source)
        claim_time = NOW - timedelta(minutes=5)
        lease = service.claim_next(
            owner_id="worker:wm", message_id=source.message_id,
            now=claim_time, lease_duration=timedelta(minutes=10),
        )
        assert lease is not None
        claimed = service.delivery(delivery.delivery_id)
        assert claimed.status is DeliveryStatus.CLAIMED
        before = tuple(store.events())
        with pytest.raises(StaleBoundaryClaimError, match="claim clock regressed"):
            service.consume(
                lease=lease, registry=consumers,
                now=claim_time - timedelta(seconds=1),
            )
        assert service.delivery(delivery.delivery_id) == claimed
        assert service.consumption(delivery.delivery_id) is None
        assert tuple(store.events()) == before
        receipt = service.consume(
            lease=lease, registry=consumers, now=claim_time,
        )
        observed = observe(spec, store, manifests, consumers, source.message_id)
        assert observed.records[0].consumption_id == receipt.consumption_id
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert observe(spec, reopened, manifests, consumers, source.message_id) == observed



@pytest.mark.parametrize("record", ("message", "delivery", "ack"))
@pytest.mark.parametrize("wrong_version", (True, 1.0))
def test_selected_protocol_rejects_boolean_and_float_contract_versions(
    record, wrong_version,
):
    """Python bool/float equal 1 numerically but not as protocol evidence."""
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    source = message()
    service = BoundaryService(store)
    if record == "message":
        invalid_message = replace(source, contract_version=wrong_version)
        with store.transaction() as uow:
            uow.save_boundary_message(invalid_message)
            uow.save_boundary_delivery(BoundaryDelivery.pending(invalid_message))
        expected = "selected boundary publication requires integer contract version"
    else:
        delivery = service.publish(source)
        if record == "delivery":
            with store.transaction() as uow:
                uow.save_boundary_delivery(replace(
                    delivery, contract_version=wrong_version,
                ))
            expected = "selected boundary delivery requires integer contract version"
        else:
            lease = service.claim_next(
                owner_id="worker:wm", message_id=source.message_id,
                now=NOW, lease_duration=timedelta(minutes=5),
            )
            assert lease is not None
            from sose.composition.model import BoundaryConsumption
            with store.transaction() as uow:
                claimed = uow.get_boundary_delivery(delivery.delivery_id)
                assert claimed.status is DeliveryStatus.CLAIMED
                uow.save_boundary_delivery(replace(
                    claimed, status=DeliveryStatus.CONSUMED,
                    consumed_at=NOW, consumer_effect_id="opaque:effect:id",
                ))
                # Persist a malformed historical receipt directly. The public
                # service correctly prevents replacing an existing immutable
                # ACK, so insert only this deliberately corrupted first ACK.
                uow.save_boundary_consumption(replace(
                    BoundaryConsumption.create(
                        delivery=claimed, consumer_effect_id="opaque:effect:id",
                        consumed_at=NOW,
                    ),
                    contract_version=wrong_version,
                ))
            expected = "selected boundary ACK requires integer contract version"
    with pytest.raises(ValueError, match=expected):
        observe(spec, store, manifests, consumers, source.message_id)


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("owner_id", ("", "   ", "\t\n", None, 123))
def test_public_boundary_claim_rejects_noncanonical_owner_before_writing(
    adapter, owner_id, tmp_path,
):
    path = tmp_path / "owner-claim-invalid.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    source = message()
    try:
        service = BoundaryService(store)
        delivery = service.publish(source)
        before = tuple(store.events())
        with pytest.raises(ValueError, match="owner_id must be a nonblank string"):
            service.claim_next(
                owner_id=owner_id,
                message_id=source.message_id,
                now=NOW, lease_duration=timedelta(minutes=10),
            )
        assert service.delivery(delivery.delivery_id) == delivery
        assert service.consumption(delivery.delivery_id) is None
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert BoundaryService(recovered).delivery(delivery.delivery_id) == delivery



@pytest.mark.parametrize("invalid_effect", (True, 123, "   "))
@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_consume_rejects_nonstr_effect_without_persisting_an_ack(
    invalid_effect, adapter, tmp_path,
):
    """A truthy bool/int or whitespace identity cannot escape the writer."""
    path = tmp_path / "bad-handler-effect.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    spec, manifests, _ = configuration()
    source = message()
    registry = BoundaryConsumerRegistry()
    registry.register(
        source_domain="warehouse_fulfillment",
        destination_domain="warehouse_management",
        contract_name=CONTRACT, contract_version=1,
        handler=lambda _source, _uow: invalid_effect,
    )
    try:
        service = BoundaryService(store)
        delivery = service.publish(source)
        lease = service.claim_next(
            owner_id="worker:wm", message_id=source.message_id,
            now=NOW, lease_duration=timedelta(minutes=5),
        )
        assert lease is not None
        claimed = service.delivery(delivery.delivery_id)
        before = tuple(store.events())
        with pytest.raises(ValueError, match="consumer effect identity must be a nonblank string"):
            service.consume(lease=lease, registry=registry, now=NOW)
        assert service.delivery(delivery.delivery_id) == claimed
        assert service.consumption(delivery.delivery_id) is None
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert BoundaryService(reopened).consumption(delivery.delivery_id) is None


@pytest.mark.parametrize("invalid_effect", (True, 123, "   "))
@pytest.mark.parametrize("corruption", ("delivery", "ack"))
def test_observer_rejects_malformed_legacy_consumer_effect_ids(
    invalid_effect, corruption,
):
    """Legacy storage can contain type-invalid records despite writer hardening."""
    from sose.composition.model import BoundaryConsumption

    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    source = message()
    service = BoundaryService(store)
    delivery = service.publish(source)
    lease = service.claim_next(
        owner_id="worker:wm", message_id=source.message_id,
        now=NOW, lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    with store.transaction() as uow:
        claimed = uow.get_boundary_delivery(delivery.delivery_id)
        delivery_effect = invalid_effect if corruption == "delivery" else "effect:valid"
        receipt_effect = invalid_effect if corruption == "ack" else "effect:valid"
        uow.save_boundary_delivery(replace(
            claimed, status=DeliveryStatus.CONSUMED,
            consumed_at=NOW, consumer_effect_id=delivery_effect,
        ))
        uow.save_boundary_consumption(
            BoundaryConsumption.create(
                delivery=claimed, consumer_effect_id=receipt_effect,
                consumed_at=NOW,
            )
        )
    expected = (
        "selected delivery has invalid consumer effect identity"
        if corruption == "delivery"
        else "selected ACK has invalid consumer effect identity"
    )
    with pytest.raises(ValueError, match=expected):
        observe(spec, store, manifests, consumers, source.message_id)



@pytest.mark.parametrize("adapter", ("sqlite", "sqlite_incremental"))
def test_boundary_observer_reads_durable_sqlite_without_writer_fence_or_mutation(
    adapter, tmp_path,
):
    """#542: a reader sees committed remote writes without claiming writer epoch."""
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    path = tmp_path / (adapter + "-fence-neutral-observation.sqlite")
    factory = SQLitePersistence if adapter == "sqlite" else SQLiteIncrementalPersistence
    spec, manifests, consumers = configuration()
    first, second = message(occurrence="epoch:1"), message(occurrence="epoch:2")
    store = factory(path)
    try:
        # Establish a real committed boundary before claiming writer authority.
        BoundaryService(store).publish(first)
        epoch = (
            store.claim_writer("writer:authorized", expected_epoch=0).epoch
            if adapter == "sqlite_incremental" else None
        )
        before_changes = store._connection.total_changes
        original = observe(spec, store, manifests, consumers, first.message_id)
        assert original.records[0].status is DeliveryStatus.PENDING
        assert store._connection.total_changes == before_changes
        if adapter == "sqlite_incremental":
            assert store.writer_epoch() == epoch

        # Independent process/connection commits a second publication.
        with factory(path) as writer:
            if adapter == "sqlite_incremental":
                with writer.transaction(owner_epoch=epoch) as uow:
                    uow.save_boundary_message(second)
                    uow.save_boundary_delivery(BoundaryDelivery.pending(second))
            else:
                BoundaryService(writer).publish(second)

        before_read_changes = store._connection.total_changes
        observed = observe(
            spec, store, manifests, consumers, first.message_id, second.message_id,
        )
        assert len(observed.records) == 2
        assert {r.message_id for r in observed.records} == {
            first.message_id, second.message_id,
        }
        assert store._connection.total_changes == before_read_changes
        assert observed == observe(
            spec, store, manifests, consumers, first.message_id, second.message_id,
        )
        assert store._connection.total_changes == before_read_changes
    finally:
        store.close()
    with factory(path) as recovered:
        assert observe(
            spec, recovered, manifests, consumers, first.message_id, second.message_id,
        ) == observed
        if adapter == "sqlite_incremental":
            assert recovered.writer_epoch() == epoch


def test_in_memory_protocol_snapshot_does_not_persist_a_fork():
    store = MemoryPersistence()
    spec, manifests, consumers = configuration()
    first = message(occurrence="memory:read-only")
    BoundaryService(store).publish(first)
    original = store._state
    records = store.boundary_protocol_snapshot()
    assert len(records[0]) == len(records[1]) == 1
    assert len(records[2]) == 0
    assert store._state is original
    assert observe(spec, store, manifests, consumers, first.message_id).records[0].status is DeliveryStatus.PENDING
    assert store._state is original



@pytest.mark.parametrize("adapter", ("jsonl", "duckdb"))
def test_durable_snapshot_refreshes_after_independent_writer_commits(
    adapter, tmp_path,
):
    """Long-lived readers cannot present an obsolete state as durable truth."""
    from sose.persistence.jsonl_journal import JSONLJournalPersistence
    from sose.persistence.duckdb import DuckDBPersistence

    factory = JSONLJournalPersistence if adapter == "jsonl" else DuckDBPersistence
    path = tmp_path / ("observer-stale-writer" + (".jsonl" if adapter == "jsonl" else ".duckdb"))
    spec, manifests, consumers = configuration()
    first = message(occurrence=adapter + ":first")
    second = message(occurrence=adapter + ":remote")
    reader = factory(path)
    try:
        writer = factory(path)
        try:
            BoundaryService(writer).publish(first)
        finally:
            if adapter == "duckdb":
                writer.close()
        initial = observe(spec, reader, manifests, consumers, first.message_id)
        assert initial.records[0].status is DeliveryStatus.PENDING

        writer = factory(path)
        try:
            BoundaryService(writer).publish(second)
        finally:
            if adapter == "duckdb":
                writer.close()

        # A coherent read must refresh from the journal or database without
        # modifying this reader's durable writer index or cached state.
        previous_state = reader._state
        previous_count = getattr(reader, "_transaction_count", None)
        evidence = observe(
            spec, reader, manifests, consumers, first.message_id, second.message_id,
        )
        assert {row.message_id for row in evidence.records} == {
            first.message_id, second.message_id,
        }
        assert reader._state is previous_state
        assert getattr(reader, "_transaction_count", None) == previous_count
        assert evidence == observe(
            spec, reader, manifests, consumers, first.message_id, second.message_id,
        )
    finally:
        if adapter == "duckdb":
            reader.close()

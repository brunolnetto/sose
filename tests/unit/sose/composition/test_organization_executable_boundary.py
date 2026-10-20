"""#496 W4a: independently advanced real domain jobs and staged causal ingress.

Logistics ingress provenance is *only* a test-scoped hypothetical manifest:
this does NOT upgrade production PC5 Logistics to audited PC6 ingress.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.model import BoundaryDelivery, DeliveryStatus
from sose.composition.organization import (
    BoundaryBinding, OrganizationDefinition, ProcessComponent, ScheduledJob,
)
from sose.composition.organization_execution import AdmittedOrganizationExecutor
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import ProcessEvidence, builtin_process_manifests
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

CONTRACT = "warehouse.dispatch_ready"
WF = "owner:warehouse-fulfillment"
LG = "owner:logistics"
NOW = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)


def backend(at):
    return SimPyBackend(origin=at)


def topology():
    return OrganizationDefinition(
        organization_id="organization:bounded-executable", version=1,
        components=(
            ProcessComponent("fulfillment", "warehouse_fulfillment", WF,
                             ("FulfillmentOrderChart",)),
            ProcessComponent("logistics", "logistics", LG, ("ShipmentChart",)),
        ),
        boundaries=(BoundaryBinding("fulfillment", "logistics", CONTRACT, 1),),
        jobs=(
            ScheduledJob(WF, "fulfillment", "run:fulfillment"),
            ScheduledJob(LG, "logistics", "run:logistics"),
        ),
    )


def hypothetical_ingress():
    manifests = builtin_process_manifests()
    entry = manifests["logistics"]
    evidence_file = "tests/unit/sose/composition/test_organization_executable_boundary.py"
    manifests["logistics"] = replace(
        entry,
        evidence=entry.evidence | {ProcessEvidence.INGRESS_CONTRACTS},
        ingress_contracts=frozenset({CONTRACT + ".v1"}),
        evidence_sources={
            **entry.evidence_sources,
            ProcessEvidence.INGRESS_CONTRACTS: (evidence_file,),
        },
        contract_provenance=(("ingress", CONTRACT + ".v1", (evidence_file,)),),
    )
    return manifests


def suite(store, *, wrong_handler=None):
    catalog = builtin_catalog()
    wf = SimulationJob(job_id=WF, definition=catalog.get("warehouse_fulfillment"),
                       persistence=store, backend_factory=backend)
    lg = SimulationJob(job_id=LG, definition=catalog.get("logistics"),
                       persistence=store, backend_factory=backend)
    original = lg.initialize({
        "start_at": datetime(2027, 2, 1, 18, tzinfo=timezone.utc),
        "pickup_delay": timedelta(hours=1),
    })
    source = wf.initialize()
    shipment_id = original.bootstrap_state.shipment_id
    registry = BoundaryConsumerRegistry()

    def valid_handler(message, uow):
        effect_id = deterministic_id(
            "composition-domain-intent", message.message_id,
            "composition.deliver_shipment", "shipment", shipment_id,
        )
        command = Command(
            command_id=effect_id, name="composition.deliver_shipment",
            entity_type="shipment", entity_id=shipment_id,
            due_at=message.produced_at, issued_at=message.produced_at,
            owner_id=LG, causation_id=message.message_id,
            correlation_id=message.correlation_id,
            payload={
                **message.payload(), "boundary_message_id": message.message_id,
                "boundary_contract": message.contract_key,
            },
        )
        existing = uow.get_command(effect_id)
        if existing is not None and existing != command:
            raise ValueError("conflicting original staged command")
        if existing is None:
            uow.save_command(command)
        return effect_id

    registry.register(
        source_domain="warehouse_fulfillment",
        destination_domain="logistics",
        contract_name=CONTRACT, contract_version=1,
        handler=valid_handler if wrong_handler is None else wrong_handler,
    )
    executor = AdmittedOrganizationExecutor(
        definition=topology(), persistence=store, jobs={WF: wf, LG: lg},
        catalog=catalog, manifests=hypothetical_ingress(), consumers=registry,
    )
    return executor, wf, lg, source, original, valid_handler


def advance_fulfillment(executor, wf, store):
    before_lg = store.job_state(LG)
    for step in range(1, 6):
        executor.run_job(
            job_id=WF, trigger_id=f"run:fulfillment:{step}",
        )
    after_lg = store.job_state(LG)
    assert before_lg == after_lg  # No global organizational tick.
    events = [
        event for event in store.events()
        if event.owner_id == WF
        and event.entity_type == "warehouse_fulfillment_order"
        and event.name == "entity.state_transition"
        and event.payload.get("to_state") == "shipped"
    ]
    assert len(events) == 1
    return events[0]


def publish_shipment(executor, event, shipment_id):
    return executor.publish(
        producer_id="fulfillment", consumer_id="logistics",
        source_event_id=event.event_id,
        contract_name=CONTRACT, contract_version=1,
        occurrence_key="dispatch:" + event.event_id,
        produced_at=event.occurred_at,
        payload={"shipment_id": shipment_id},
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_real_owner_local_jobs_to_typed_event_staged_command_ack_and_terminal_certificate(adapter, tmp_path):
    path = tmp_path / "bounded-organization.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        executor, wf, lg, source, shipment, _ = suite(store)
        event = advance_fulfillment(executor, wf, store)
        message, delivery = publish_shipment(
            executor, event, shipment.bootstrap_state.shipment_id,
        )
        replay_message, replay_delivery = publish_shipment(
            executor, event, shipment.bootstrap_state.shipment_id,
        )
        assert (message, delivery) == (replay_message, replay_delivery)
        assert message.causation_kind == "event"
        assert message.causation_id == event.event_id
        assert message.correlation_id == event.correlation_id
        before_ack = store.job_state(LG)
        assert before_ack.next_tick == 0

        ack = executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
        assert ack.message_id == message.message_id
        command = store.command(ack.consumer_effect_id)
        assert command is not None
        assert command.owner_id == LG
        assert command.causation_id == message.message_id
        assert store.business_effect(ack.consumer_effect_id) is None
        with pytest.raises(ValueError, match="terminal transition not attested after ACK"):
            executor.certify(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id,
                effect_id=ack.consumer_effect_id, completed_at=event.occurred_at,
            )
        assert store.business_effect(ack.consumer_effect_id) is None
        assert store.command(ack.consumer_effect_id) == command

        # Advance JUST the independently owned downstream real SimulationJob.
        result = executor.run_job(
            job_id=LG, trigger_id="run:logistics:1",
        )
        assert result.job_id == LG
        assert store.job_state(WF).next_tick == 5
        assert store.job_state(LG).next_tick == 1
        entity = store.entity("shipment", shipment.bootstrap_state.shipment_id)
        assert entity.state == "delivered"

        certificate = executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id,
            effect_id=ack.consumer_effect_id,
            completed_at=store.job_state(LG).logical_time,
        )
        assert certificate.effect_id == ack.consumer_effect_id
        assert certificate.boundary_message_id == message.message_id
        assert certificate.correlation_id == message.correlation_id
        assert store.business_effect(ack.consumer_effect_id) == certificate
        assert store.command(ack.consumer_effect_id) is None
        assert executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        ) == ack
        assert executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id,
            effect_id=ack.consumer_effect_id,
            completed_at=store.job_state(LG).logical_time,
        ) == certificate
        assert BoundaryService(store).delivery(delivery.delivery_id).status is DeliveryStatus.CONSUMED
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            assert restored.business_effect(ack.consumer_effect_id) == certificate
            assert restored.command(ack.consumer_effect_id) is None
            assert restored.job_state(LG).next_tick == 1
            assert restored.job_state(WF).next_tick == 5


@pytest.mark.parametrize("invalid", ("missing_command", "wrong_owner", "wrong_cause"))
def test_wrong_causal_handler_rolls_back_ack_even_after_durable_claim(invalid):
    store = MemoryPersistence()

    def broken(message, uow):
        command_id = "command:malicious"
        if invalid == "missing_command":
            return command_id
        uow.save_command(Command(
            command_id=command_id, name="composition.deliver_shipment",
            entity_type="shipment", entity_id="shipment:fake",
            due_at=message.produced_at, issued_at=message.produced_at,
            owner_id="owner:forged" if invalid == "wrong_owner" else LG,
            causation_id="unrelated" if invalid == "wrong_cause" else message.message_id,
            correlation_id=message.correlation_id,
        ))
        return command_id

    executor, wf, _, _, shipment, _ = suite(store, wrong_handler=broken)
    event = advance_fulfillment(executor, wf, store)
    message, delivery = publish_shipment(
        executor, event, shipment.bootstrap_state.shipment_id,
    )
    with pytest.raises(ValueError, match="lacks durable staged Command|owner or causal"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=1),
        )
    persisted = BoundaryService(store).delivery(delivery.delivery_id)
    assert persisted.status is DeliveryStatus.CLAIMED
    assert store.command("command:malicious") is None
    assert BoundaryService(store).consumption(delivery.delivery_id) is None


def test_foreign_owned_or_missing_source_event_cannot_be_published():
    store = MemoryPersistence()
    executor, wf, _, _, shipment, _ = suite(store)
    before = tuple(store.events())
    with pytest.raises(ValueError, match="source DomainEvent missing"):
        publish_shipment(
            executor,
            DomainEvent(event_id="nonexistent", name="entity.state_transition",
                        entity_type="warehouse_fulfillment_order", entity_id="order",
                        occurred_at=NOW, owner_id=WF, correlation_id="corr",
                        payload={"trigger": "ship", "from_state": "packing",
                                 "to_state": "shipped"}),
            shipment.bootstrap_state.shipment_id,
        )
    assert tuple(store.events()) == before
    event = advance_fulfillment(executor, wf, store)
    with store.transaction() as uow:
        uow.append_event(replace(event, event_id="foreign-event", owner_id="other:org"))
    before_messages = tuple(store.events())
    with pytest.raises(ValueError, match="source owner/StateChart/causality mismatch"):
        publish_shipment(
            executor, replace(event, event_id="foreign-event"),
            shipment.bootstrap_state.shipment_id,
        )
    assert tuple(store.events()) == before_messages


def test_executor_rejects_mismatched_real_job_ownership_or_trigger_namespace():
    store = MemoryPersistence()
    ex, wf, lg, *_ = suite(store)
    with pytest.raises(ValueError, match="component trigger"):
        ex.run_job(job_id=WF, trigger_id="run:logistics:forged")
    with pytest.raises(ValueError, match="undeclared"):
        ex.run_job(job_id="foreign:job", trigger_id="anything")
    with pytest.raises(ValueError, match="owner/domain/store"):
        AdmittedOrganizationExecutor(
            definition=topology(), persistence=store,
            jobs={WF: lg, LG: wf},
            catalog=builtin_catalog(), manifests=hypothetical_ingress(),
            consumers=ex.consumers,
        )
    assert store.job_state(WF).next_tick == 0
    assert store.job_state(LG).next_tick == 0


def test_finished_before_acked_work_does_not_qualify_as_effect_certificate():
    """A terminal projection with no later attested event must never certify work."""
    store = MemoryPersistence()
    executor, wf, lg, _, shipment, _ = suite(store)
    # The logistics domain independently completes its own reference shipment
    # BEFORE the warehouse's future-dated dispatch event was published.
    from sose.examples.logistics.simulation import ORIGIN as LOGISTICS_ORIGIN
    # Construct a separate (already terminal) authority with a deliberately
    # earlier owner-local clock, not a mutable projection shortcut.
    assert LOGISTICS_ORIGIN < store.job_state(LG).logical_time
    # This makes Logistics' time earlier than Warehouse's effective event.
    # Directly using an older real domain job's historical terminal events
    # would previously pass BusinessEffectService's terminal-state-only gate.
    event = advance_fulfillment(executor, wf, store)
    msg, _ = publish_shipment(executor, event, shipment.bootstrap_state.shipment_id)
    ack = executor.consume(
        producer_id="fulfillment", consumer_id="logistics",
        message_id=msg.message_id, now=event.occurred_at,
        lease_duration=timedelta(minutes=10),
    )
    # A forged terminal entity projection is not sufficient evidence of
    # a post-ACK StateChart transition, even if the command/ACK is valid.
    with store.transaction(owner_id=LG) as uow:
        entity = uow.get_entity("shipment", shipment.bootstrap_state.shipment_id)
        entity.state = "delivered"
        uow.save_entity(entity)
    with pytest.raises(ValueError, match="terminal transition not attested after ACK"):
        executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=msg.message_id, effect_id=ack.consumer_effect_id,
            completed_at=event.occurred_at + timedelta(hours=10),
        )
    assert store.business_effect(ack.consumer_effect_id) is None
    assert store.command(ack.consumer_effect_id) is not None



# W4a coverage hardening: verify each negative gate against real durable fixtures.
# These tests are not a substitute for the W4b Command -> domain event proof.

def _published_ingress(store):
    executor, wf, lg, _, shipment, _ = suite(store)
    event = advance_fulfillment(executor, wf, store)
    message, delivery = publish_shipment(
        executor, event, shipment.bootstrap_state.shipment_id,
    )
    return executor, wf, lg, event, message, delivery


def _ack_ingress(store):
    executor, wf, lg, event, message, delivery = _published_ingress(store)
    acknowledgement = executor.consume(
        producer_id="fulfillment", consumer_id="logistics",
        message_id=message.message_id, now=event.occurred_at,
        lease_duration=timedelta(minutes=10),
    )
    return executor, event, message, delivery, acknowledgement


@pytest.mark.parametrize("invalid", ("missing_job", "extra_job", "imposter_job"))
def test_executor_rejects_incomplete_or_fake_real_job_mapping(invalid):
    store = MemoryPersistence()
    executor, wf, lg, *_ = suite(store)
    jobs = {WF: wf, LG: lg}
    if invalid == "missing_job":
        jobs.pop(LG)
    elif invalid == "extra_job":
        jobs["owner:unregistered"] = lg
    else:
        jobs[LG] = object()
    with pytest.raises(
        (ValueError, TypeError),
        match="exact declared job mapping|real SimulationJob",
    ):
        AdmittedOrganizationExecutor(
            definition=topology(), persistence=store,
            jobs=jobs, catalog=builtin_catalog(),
            manifests=hypothetical_ingress(), consumers=executor.consumers,
        )


def test_reject_unadmitted_boundary_without_creating_delivery():
    store = MemoryPersistence()
    executor, wf, _, _, shipment, _ = suite(store)
    event = advance_fulfillment(executor, wf, store)
    with pytest.raises(ValueError, match="boundary not uniquely admitted"):
        executor.publish(
            producer_id="fulfillment", consumer_id="logistics",
            source_event_id=event.event_id,
            contract_name="unregistered-contract", contract_version=1,
            occurrence_key="invalid:contract",
            produced_at=event.occurred_at,
            payload={"shipment_id": shipment.bootstrap_state.shipment_id},
        )
    with store.transaction() as uow:
        assert not uow.boundary_deliveries()


@pytest.mark.parametrize(
    "violation,match",
    (
        ("untyped", "typed durable DomainEvent"),
        ("missing_event", "source DomainEvent missing"),
        ("wrong_source_domain", "source/destination domain mismatch"),
        ("wrong_destination_domain", "source/destination domain mismatch"),
        ("wrong_identity", "source owner/StateChart/causality mismatch"),
        ("wrong_correlation", "source owner/StateChart/causality mismatch"),
        ("backdated", "source owner/StateChart/causality mismatch"),
    ),
)
def test_owned_source_gate_rejects_forged_cross_domain_claims(violation, match):
    store = MemoryPersistence()
    executor, _, _, event, message, _ = _published_ingress(store)
    if violation == "untyped":
        adversary = replace(message, causation_kind=None, causation_id=None)
    elif violation == "missing_event":
        adversary = replace(message, causation_id="event:does-not-exist")
    elif violation == "wrong_source_domain":
        adversary = replace(message, source_domain="unrelated:producer")
    elif violation == "wrong_destination_domain":
        adversary = replace(message, destination_domain="unrelated:consumer")
    elif violation == "wrong_identity":
        adversary = replace(message, source_identity="other:order")
    elif violation == "wrong_correlation":
        adversary = replace(message, correlation_id="unrelated:correlation")
    else:
        adversary = replace(
            message, produced_at=event.occurred_at - timedelta(seconds=1),
        )
    before = tuple(store.events())
    with store.transaction() as uow:
        with pytest.raises(ValueError, match=match):
            executor._check_message(uow, adversary, "fulfillment", "logistics")
    assert tuple(store.events()) == before


def test_consume_requires_durable_message_and_matching_delivery():
    store = MemoryPersistence()
    executor, _, _, event, message, delivery = _published_ingress(store)
    with pytest.raises(ValueError, match="boundary message missing"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id="boundary:absent", now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
    orphan = replace(message, message_id="boundary:orphan-delivery")
    with store.transaction() as uow:
        uow.save_boundary_message(orphan)
    with pytest.raises(ValueError, match="lacks durable delivery"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=orphan.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
    assert BoundaryService(store).delivery(delivery.delivery_id) == delivery


def test_claim_lease_blocks_duplicate_worker_and_does_not_ack_work():
    store = MemoryPersistence()
    executor, _, _, event, message, delivery = _published_ingress(store)
    claim = BoundaryService(store).claim_next(
        owner_id=LG, message_id=message.message_id,
        now=event.occurred_at, lease_duration=timedelta(hours=1),
    )
    assert claim is not None
    with pytest.raises(RuntimeError, match="not claimable"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id,
            now=event.occurred_at + timedelta(minutes=1),
            lease_duration=timedelta(minutes=5),
        )
    assert BoundaryService(store).consumption(delivery.delivery_id) is None


def test_consumed_delivery_missing_ack_is_not_reprocessed_or_released():
    store = MemoryPersistence()
    executor, _, _, event, message, delivery = _published_ingress(store)
    # Acquire an authentic owner-bound lease first. We then corrupt ONLY the
    # consumed ACK state, not the claim provenance, so this test falsifies
    # missing durable receipts instead of stopping at the earlier owner gate.
    lease = BoundaryService(store).claim_next(
        owner_id=LG, message_id=message.message_id, now=event.occurred_at,
        lease_duration=timedelta(minutes=10),
    )
    assert lease is not None
    with store.transaction() as uow:
        original_claim = uow.get_boundary_delivery(delivery.delivery_id)
        assert original_claim.status is DeliveryStatus.CLAIMED
        uow.save_boundary_delivery(replace(
            original_claim, status=DeliveryStatus.CONSUMED,
            consumed_at=event.occurred_at,
            consumer_effect_id="command:missing-ack",
        ))
    with pytest.raises(ValueError, match="consumed boundary lacks durable ACK"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
    assert BoundaryService(store).consumption(delivery.delivery_id) is None


def test_consumed_ack_without_command_or_certificate_is_not_new_authority():
    store = MemoryPersistence()
    executor, event, message, delivery, acknowledgement = _ack_ingress(store)
    with store.transaction() as uow:
        uow.delete_command(acknowledgement.consumer_effect_id)
    with pytest.raises(ValueError, match="lacks Command or certificate"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
    with pytest.raises(ValueError, match="staged Command or existing certificate"):
        executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id,
            effect_id=acknowledgement.consumer_effect_id,
            completed_at=event.occurred_at,
        )
    assert BoundaryService(store).consumption(delivery.delivery_id) == acknowledgement


def test_consumed_ack_with_pending_command_is_an_idempotent_replay():
    store = MemoryPersistence()
    executor, event, message, _, acknowledgement = _ack_ingress(store)
    assert executor.consume(
        producer_id="fulfillment", consumer_id="logistics",
        message_id=message.message_id, now=event.occurred_at,
        lease_duration=timedelta(minutes=10),
    ) == acknowledgement
    assert store.command(acknowledgement.consumer_effect_id) is not None


def test_existing_certificate_must_match_actual_ack_correlation():
    from sose.composition.effects import BusinessEffectApplied
    store = MemoryPersistence()
    executor, event, message, _, acknowledgement = _ack_ingress(store)
    fake = BusinessEffectApplied.from_applied(
        effect_id=acknowledgement.consumer_effect_id,
        boundary_message_id=message.message_id,
        correlation_id="foreign:correlation",
        entity_type="shipment", entity_id="shipment:unrelated",
        terminal_state="delivered", entity_version=1,
        completed_at=event.occurred_at,
    )
    with store.transaction() as uow:
        uow.save_business_effect(fake)
    with pytest.raises(ValueError, match="certificate contradicts ACK"):
        executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
    assert store.business_effect(acknowledgement.consumer_effect_id) == fake


@pytest.mark.parametrize("attempt", ("unknown_message", "pending_delivery", "wrong_effect"))
def test_certify_never_uses_missing_or_unconsumed_or_foreign_ack(attempt):
    store = MemoryPersistence()
    executor, _, _, event, message, _ = _published_ingress(store)
    effect_id = "effect:not-authorized"
    if attempt == "unknown_message":
        message_id = "message:nonexistent"
        match = "durable boundary message"
    else:
        message_id = message.message_id
        match = "requires consumed durable delivery"
        if attempt == "wrong_effect":
            ack = executor.consume(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id, now=event.occurred_at,
                lease_duration=timedelta(minutes=10),
            )
            assert ack.consumer_effect_id != effect_id
            match = "effect identity contradicts ACK"
    with pytest.raises(ValueError, match=match):
        executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message_id, effect_id=effect_id,
            completed_at=event.occurred_at,
        )


def test_certify_rejects_staged_command_without_terminal_contract():
    store = MemoryPersistence()
    def unsupported(message, uow):
        ident = "command:unsupported-terminal"
        uow.save_command(Command(
            command_id=ident, name="composition.not_certifiable",
            entity_type="shipment", entity_id=message.payload()["shipment_id"],
            due_at=message.produced_at, issued_at=message.produced_at,
            owner_id=LG, causation_id=message.message_id,
            correlation_id=message.correlation_id,
        ))
        return ident
    executor, wf, _, _, shipment, _ = suite(store, wrong_handler=unsupported)
    event = advance_fulfillment(executor, wf, store)
    message, _ = publish_shipment(executor, event, shipment.bootstrap_state.shipment_id)
    ack = executor.consume(
        producer_id="fulfillment", consumer_id="logistics",
        message_id=message.message_id, now=event.occurred_at,
        lease_duration=timedelta(minutes=10),
    )
    with pytest.raises(ValueError, match="no certified terminal"):
        executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, effect_id=ack.consumer_effect_id,
            completed_at=event.occurred_at + timedelta(hours=3),
        )
    assert store.command(ack.consumer_effect_id) is not None
    assert store.business_effect(ack.consumer_effect_id) is None


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_ingress_rejects_backdated_claim_and_ack_before_source_publication(
    adapter, tmp_path,
):
    path = tmp_path / "backdated-ack.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        executor, _, _, event, message, delivery = _published_ingress(store)
        before = tuple(store.events())
        with pytest.raises(ValueError, match="cannot predate message production"):
            executor.consume(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id,
                now=event.occurred_at - timedelta(minutes=1),
                lease_duration=timedelta(minutes=5),
            )
        assert BoundaryService(store).delivery(delivery.delivery_id) == delivery
        assert BoundaryService(store).consumption(delivery.delivery_id) is None
        assert tuple(store.events()) == before
    finally:
        if adapter == "sqlite":
            store.close()


def test_named_domain_clone_may_not_replace_catalog_admitted_executable():
    store = MemoryPersistence()
    executor, wf, lg, *_ = suite(store)
    fake_definition = replace(
        lg.definition, build_runtime=lambda *_args: (_ for _ in ()).throw(
            AssertionError("unadmitted runtime must never execute")
        ),
    )
    fake_job = SimulationJob(
        job_id=LG, definition=fake_definition,
        persistence=store, backend_factory=backend,
    )
    with pytest.raises(ValueError, match="owner/domain/store"):
        AdmittedOrganizationExecutor(
            definition=topology(), persistence=store,
            jobs={WF: wf, LG: fake_job},
            catalog=builtin_catalog(), manifests=hypothetical_ingress(),
            consumers=executor.consumers,
        )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_replayed_certificate_cannot_bypass_post_ack_event_attestation(adapter, tmp_path):
    """A prior certificate without a real descendant never gains authority on replay."""
    from sose.composition.effects import BusinessEffectApplied

    path = tmp_path / "forged-certificate.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        executor, _, _, event, message, delivery = _published_ingress(store)
        ack = executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
        command = store.command(ack.consumer_effect_id)
        assert command is not None
        with store.transaction(owner_id=LG) as uow:
            entity = uow.get_entity(command.entity_type, command.entity_id)
            entity.state = "delivered"
            uow.save_entity(entity)
        forged = BusinessEffectApplied.from_applied(
            effect_id=ack.consumer_effect_id,
            boundary_message_id=message.message_id,
            correlation_id=message.correlation_id,
            entity_type=command.entity_type, entity_id=command.entity_id,
            terminal_state="delivered",
            entity_version=store.entity(command.entity_type, command.entity_id).version,
            completed_at=event.occurred_at + timedelta(hours=10),
        )
        with store.transaction() as uow:
            uow.save_business_effect(forged)
            uow.delete_command(ack.consumer_effect_id)
        with pytest.raises(ValueError, match="terminal transition not attested after ACK"):
            executor.certify(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id, effect_id=ack.consumer_effect_id,
                completed_at=forged.completed_at,
            )
        with pytest.raises(ValueError, match="terminal transition not attested after ACK"):
            executor.consume(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id, now=event.occurred_at,
                lease_duration=timedelta(minutes=10),
            )
        assert store.business_effect(ack.consumer_effect_id) == forged
        assert BoundaryService(store).delivery(delivery.delivery_id).status is DeliveryStatus.CONSUMED
    finally:
        if adapter == "sqlite":
            store.close()



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
@pytest.mark.parametrize("violation", ("foreign_claim_owner", "backdated_durable_ack"))
def test_preexisting_consumed_receipt_must_have_real_owner_and_causal_time(
    adapter, violation, tmp_path,
):
    """Even a valid staged Command cannot launder a foreign or pre-source ACK.

    Direct BoundaryService is intentionally used to reproduce historical
    data/external workers that bypassed the bounded executor's ingress.
    """
    path = tmp_path / ("corrupt-" + violation + ".sqlite")
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        executor, _, _, event, message, delivery = _published_ingress(store)
        original_time = event.occurred_at
        claim_time = (
            original_time - timedelta(minutes=2)
            if violation == "backdated_durable_ack" else original_time
        )
        claimant = "worker:foreign-org" if violation == "foreign_claim_owner" else LG
        service = BoundaryService(store)
        lease = service.claim_next(
            owner_id=claimant, message_id=message.message_id,
            now=claim_time, lease_duration=timedelta(minutes=10),
        )
        assert lease is not None
        receipt = service.consume(
            lease=lease, registry=executor.consumers, now=claim_time,
        )
        assert receipt.consumer_effect_id
        assert store.command(receipt.consumer_effect_id) is not None
        before = tuple(store.events())
        expected = (
            "claim owner contradicts admitted consumer"
            if violation == "foreign_claim_owner"
            else "ACK predates message production or claim"
        )
        with pytest.raises(ValueError, match=expected):
            executor.consume(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id, now=original_time,
                lease_duration=timedelta(minutes=10),
            )
        with pytest.raises(ValueError, match=expected):
            executor.certify(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id,
                effect_id=receipt.consumer_effect_id,
                completed_at=original_time + timedelta(hours=10),
            )
        assert store.business_effect(receipt.consumer_effect_id) is None
        assert tuple(store.events()) == before
        assert service.delivery(delivery.delivery_id).status is DeliveryStatus.CONSUMED
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as recovered:
            assert recovered.business_effect(receipt.consumer_effect_id) is None
            assert BoundaryService(recovered).consumption(delivery.delivery_id) == receipt



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_terminal_attestation_and_business_certificate_share_one_uow(
    adapter, tmp_path, monkeypatch,
):
    """#543: prevent commit-window TOCTOU between causal check and certificate."""
    path = tmp_path / "atomic-organization-certificate.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        executor, wf, lg, _, shipment, _ = suite(store)
        event = advance_fulfillment(executor, wf, store)
        message, _ = publish_shipment(
            executor, event, shipment.bootstrap_state.shipment_id,
        )
        ack = executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
        executor.run_job(job_id=LG, trigger_id="run:logistics:1")
        now = store.job_state(LG).logical_time
        called = []
        original = type(store).transaction

        def tracked(self, *args, **kwargs):
            if self is store:
                called.append("transaction")
            return original(self, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(type(store), "transaction", tracked)
            certified = executor.certify(
                producer_id="fulfillment", consumer_id="logistics",
                message_id=message.message_id,
                effect_id=ack.consumer_effect_id, completed_at=now,
            )
        assert called == ["transaction"], (
            "temporal attestation and certificate must never use separate UoWs"
        )
        assert certified == store.business_effect(ack.consumer_effect_id)
        assert store.command(ack.consumer_effect_id) is None
    finally:
        if adapter == "sqlite":
            store.close()


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_certificate_write_failure_rolls_back_validated_effect_and_command(
    adapter, tmp_path, monkeypatch,
):
    """An exception after a staged certificate must not commit a half-effect."""
    from sose.composition.effects import BusinessEffectApplied

    path = tmp_path / "atomic-certificate-rollback.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        executor, wf, lg, _, shipment, _ = suite(store)
        event = advance_fulfillment(executor, wf, store)
        message, delivery = publish_shipment(
            executor, event, shipment.bootstrap_state.shipment_id,
        )
        ack = executor.consume(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, now=event.occurred_at,
            lease_duration=timedelta(minutes=10),
        )
        executor.run_job(job_id=LG, trigger_id="run:logistics:1")
        completion_time = store.job_state(LG).logical_time
        pending = store.command(ack.consumer_effect_id)
        assert pending is not None

        def commit_then_fault(uow, *, effect_id, completed_at):
            entity = uow.get_entity(pending.entity_type, pending.entity_id)
            uow.save_business_effect(BusinessEffectApplied.from_applied(
                effect_id=effect_id, boundary_message_id=message.message_id,
                correlation_id=message.correlation_id,
                entity_type=entity.entity_type, entity_id=entity.id,
                terminal_state=entity.state, entity_version=entity.version,
                completed_at=completed_at,
            ))
            uow.delete_command(effect_id)
            raise RuntimeError("injected certificate write interruption")

        with monkeypatch.context() as patch:
            patch.setattr(executor._business, "_complete_in_uow", commit_then_fault)
            with pytest.raises(RuntimeError, match="certificate write interruption"):
                executor.certify(
                    producer_id="fulfillment", consumer_id="logistics",
                    message_id=message.message_id, effect_id=ack.consumer_effect_id,
                    completed_at=completion_time,
                )
        assert store.business_effect(ack.consumer_effect_id) is None
        assert store.command(ack.consumer_effect_id) == pending
        assert BoundaryService(store).delivery(delivery.delivery_id).status is DeliveryStatus.CONSUMED
        certified = executor.certify(
            producer_id="fulfillment", consumer_id="logistics",
            message_id=message.message_id, effect_id=ack.consumer_effect_id,
            completed_at=completion_time,
        )
        assert store.business_effect(ack.consumer_effect_id) == certified
        assert store.command(ack.consumer_effect_id) is None
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as restored:
            assert restored.business_effect(ack.consumer_effect_id) == certified

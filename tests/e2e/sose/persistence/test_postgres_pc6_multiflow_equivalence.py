"""Multi-flow PostgreSQL causal equivalence under independent-worker faults.

This is a PC6 boundary/business-effect protocol experiment, not an assertion
that arbitrary domain engines or external payment processors are exactly-once.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import json
import os

import pytest

from sose.composition.audit import audit_causal_history
from sose.composition.bindings import CustomerSettlementBinding, SettlementBindingService
from sose.composition.boundary import (
    BoundaryConsumerRegistry, BoundaryService, StaleBoundaryClaimError,
)
from sose.composition.effects import BusinessEffectService
from sose.composition.model import BoundaryMessage, DeliveryStatus
from sose.core.events import Command, DomainEvent
from sose.domain.entity import Entity
from sose.examples.order_to_cash.simulation import receivable_id
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")
ORIGIN = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
FLOWS = ("a", "b")
STAGES = (
    ("warehouse.dispatch_ready", "warehouse_fulfillment", "logistics",
     "composition.deliver_shipment", "shipment", "delivered"),
    ("logistics.delivery_completed", "logistics", "order_to_cash",
     "composition.complete_external_fulfillment", "sales_order", "invoiced"),
    ("o2c.payment_requested", "order_to_cash", "cards_payments",
     "composition.settle_customer_payment", "card_payment", "settled"),
    ("accounting.entry_requested", "cards_payments", "record_to_report",
     "composition.post_customer_journal", "journal_entry", "posted"),
)


def _entity_id(flow, kind):
    return {
        "shipment": f"shipment-{flow}",
        "sales_order": f"order-{flow}",
        "card_payment": f"payment-{flow}",
        "journal_entry": f"journal-{flow}",
    }[kind]


def _effect_id(message):
    payload = message.payload()
    return f"pc6-effect-{payload['flow']}-{payload['stage']}"


def _message(flow, stage, parent):
    contract, origin_domain, destination, _, _, _ = STAGES[stage]
    payload = {
        "flow": flow, "stage": stage, "order_id": f"order-{flow}",
        "shipment_id": f"shipment-{flow}", "payment_id": f"payment-{flow}",
        "journal_id": f"journal-{flow}", "amount": 250.0, "currency": "USD",
    }
    return BoundaryMessage.create(
        contract_name=contract, contract_version=1,
        source_domain=origin_domain, source_identity=f"{origin_domain}-{flow}",
        destination_domain=destination, occurrence_key=f"pc6-{stage}",
        correlation_id=f"pc6-flow-{flow}",
        causation_kind="boundary" if parent is not None else None,
        causation_id=parent.message_id if parent is not None else None,
        produced_at=ORIGIN + timedelta(minutes=stage),
        payload=payload,
    )


def _registry():
    registry = BoundaryConsumerRegistry()
    for stage, (contract, _, destination, command_name, kind, _) in enumerate(STAGES):
        def accept(message, uow, *, stage=stage, command_name=command_name, kind=kind):
            if message.payload()["stage"] != stage:
                raise ValueError("contract and stage disagree")
            flow = message.payload()["flow"]
            command = Command(
                command_id=_effect_id(message), name=command_name,
                entity_type=kind, entity_id=_entity_id(flow, kind),
                due_at=message.produced_at, issued_at=message.produced_at,
                causation_id=message.message_id,
                correlation_id=message.correlation_id,
            )
            existing = uow.get_command(command.command_id)
            if existing is None:
                uow.save_command(command)
            elif existing != command:
                raise ValueError("incompatible repeated business effect")
            return command.command_id

        registry.register(
            destination_domain=destination, contract_name=contract,
            contract_version=1, handler=accept,
        )
    return registry


def _seed(store):
    with store.transaction() as uow:
        for flow in FLOWS:
            for kind, initial in (
                ("shipment", "created"),
                ("sales_order", "submitted"),
                ("card_payment", "authorization_requested"),
                ("journal_entry", "drafted"),
            ):
                uow.save_entity(Entity(
                    id=_entity_id(flow, kind), entity_type=kind, state=initial,
                    attributes={"amount": 250.0, "currency": "USD"},
                ))
    for flow in FLOWS:
        SettlementBindingService(store).bind(CustomerSettlementBinding.create(
            order_id=f"order-{flow}", payment_id=f"payment-{flow}",
            journal_id=f"journal-{flow}", amount=250.0, currency="USD",
            correlation_id=f"pc6-flow-{flow}",
        ))


def _apply_once(store, message):
    """Durable reference outcome; retry must not duplicate its domain event."""
    flow, stage = message.payload()["flow"], message.payload()["stage"]
    _, _, _, _, kind, terminal = STAGES[stage]
    entity_id = _entity_id(flow, kind)
    event_id = f"pc6-applied-{flow}-{stage}"
    with store.transaction() as uow:
        existing = uow.get_event(event_id)
        if existing is not None:
            assert existing.causation_id == message.message_id
            return
        entity = uow.get_entity(kind, entity_id)
        assert entity is not None and entity.state != terminal
        uow.save_entity(replace(
            entity, state=terminal, version=entity.version + 1,
        ))
        if stage == 1:
            uow.save_entity(Entity(
                id=receivable_id(f"order-{flow}"), entity_type="receivable",
                state="open", attributes={
                    "order_id": f"order-{flow}", "amount": 250.0,
                    "currency": "USD",
                },
            ))
        uow.append_event(DomainEvent(
            event_id=event_id, name=f"pc6.business_applied.{stage}",
            entity_type=kind, entity_id=entity_id,
            occurred_at=message.produced_at,
            correlation_id=message.correlation_id,
            causation_id=message.message_id,
            payload={"stage": stage, "flow": flow},
        ))


def _certify(store, message):
    return BusinessEffectService(store).complete(
        effect_id=_effect_id(message),
        completed_at=message.produced_at + timedelta(seconds=1),
    )


def _claim_pair(stores, stage):
    gate = Barrier(2)
    destination = STAGES[stage][2]
    def claim(index):
        gate.wait(timeout=10)
        return index, BoundaryService(stores[index]).claim_next(
            owner_id=f"worker-{index}", now=ORIGIN + timedelta(minutes=stage),
            lease_duration=timedelta(seconds=2),
            destination_domain=destination,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert all(lease is not None for _, lease in results)
    assert len({lease.message_id for _, lease in results}) == 2
    return results


def _semantic_view(store):
    """Take this view only after both workers quiesce, on a fresh connection."""
    audit = audit_causal_history(store)
    deliveries = store.boundary_deliveries()
    assert all(d.status is DeliveryStatus.CONSUMED for d in deliveries)
    # Exclude physical lease attempt/epoch/owner/timestamp, not business facts.
    with store.boundary_transaction() as uow:
        consumed = tuple(sorted(
            (
                d.message_id, d.consumer_effect_id,
                uow.get_boundary_consumption(d.delivery_id).consumption_id,
            ) for d in deliveries
        ))
        pending = tuple(sorted(
            proof.effect_id for proof in store.business_effects()
            if uow.get_command(proof.effect_id) is not None
        ))
    def norm(values):
        return tuple(sorted(json.dumps(
            asdict(v), default=str, sort_keys=True, separators=(",", ":"),
        ) for v in values))
    return {
        "dag": audit.semantic_digest, "edges": audit.typed_edges,
        "messages": audit.message_count, "applied": audit.applied_effects,
        "pending_effects": audit.pending_effects, "pending_commands": pending,
        "entities": norm(store.entities()), "events": norm(store.events()),
        "receipts": norm(store.business_effects()), "consumptions": consumed,
        "deliveries": tuple(sorted(
            (d.delivery_id, d.message_id, d.status.value, d.consumer_effect_id)
            for d in deliveries
        )),
        "bindings": tuple(
            asdict(SettlementBindingService(store).for_order(f"order-{flow}"))
            for flow in FLOWS
        ),
        "scheduled": norm(store.scheduled_work()),
        "resource_reservations": norm(store.resource_reservations()),
        "store_items": norm(store.store_items()),
        "container_states": norm(store.container_states()),
        "checkpoints": norm(store.sink_checkpoints()),
        "job_states": norm(store.job_states()),
    }


def _execute(namespace, *, inject_faults):
    stores = [PostgresPersistence(DSN, namespace=namespace) for _ in range(2)]
    messages = {}
    registry = _registry()

    def restart():
        for store in stores:
            store.close()
        stores[:] = [PostgresPersistence(DSN, namespace=namespace) for _ in range(2)]

    try:
        _seed(stores[0])
        for stage in range(len(STAGES)):
            for flow in FLOWS:
                parent = messages.get((flow, stage - 1))
                message = _message(flow, stage, parent)
                BoundaryService(stores[0]).publish(message)
                messages[(flow, stage)] = message
            claims = _claim_pair(stores, stage)
            by_flow = {
                messages[(flow, stage)].message_id: flow for flow in FLOWS
            }

            if inject_faults and stage == 0:
                # Worker A dies after CLAIM; worker B ACKs and applies state,
                # but dies before certifying. The two failures leave distinct
                # authoritative recovery predicates in the same namespace.
                a_index, a_lease = next(
                    (i, lease) for i, lease in claims
                    if by_flow[lease.message_id] == "a"
                )
                b_index, b_lease = next(
                    (i, lease) for i, lease in claims
                    if by_flow[lease.message_id] == "b"
                )
                b_msg = messages[("b", stage)]
                BoundaryService(stores[b_index]).consume(
                    lease=b_lease, registry=registry,
                    now=b_msg.produced_at,
                )
                _apply_once(stores[b_index], b_msg)
                restart()
                reclaimed = BoundaryService(stores[0]).claim_next(
                    owner_id="recovery-worker", now=ORIGIN + timedelta(seconds=3),
                    lease_duration=timedelta(minutes=1),
                    destination_domain=STAGES[stage][2],
                )
                assert reclaimed is not None and reclaimed.message_id == a_lease.message_id
                assert reclaimed.epoch == a_lease.epoch + 1
                with pytest.raises(StaleBoundaryClaimError):
                    BoundaryService(stores[1]).consume(
                        lease=a_lease, registry=registry,
                        now=ORIGIN + timedelta(seconds=3),
                    )
                a_msg = messages[("a", stage)]
                BoundaryService(stores[0]).consume(
                    lease=reclaimed, registry=registry,
                    now=ORIGIN + timedelta(seconds=3),
                )
                _apply_once(stores[0], a_msg)
                _certify(stores[0], a_msg)
                _apply_once(stores[1], b_msg)  # State was already committed.
                _certify(stores[1], b_msg)
            elif inject_faults and stage == 2:
                # Worker death after durable ACK but before domain application.
                for index, lease in claims:
                    msg = next(m for m in (
                        messages[(flow, stage)] for flow in FLOWS
                    ) if m.message_id == lease.message_id)
                    BoundaryService(stores[index]).consume(
                        lease=lease, registry=registry, now=msg.produced_at,
                    )
                    if msg.payload()["flow"] == "b":
                        _apply_once(stores[index], msg)
                        _certify(stores[index], msg)
                restart()
                a_msg = messages[("a", stage)]
                assert stores[0].command(_effect_id(a_msg)) is not None
                _apply_once(stores[0], a_msg)
                _certify(stores[0], a_msg)
            else:
                for index, lease in claims:
                    msg = next(m for m in (
                        messages[(flow, stage)] for flow in FLOWS
                    ) if m.message_id == lease.message_id)
                    BoundaryService(stores[index]).consume(
                        lease=lease, registry=registry, now=msg.produced_at,
                    )
                    _apply_once(stores[index], msg)
                    _certify(stores[index], msg)

        # Repeated certificate finalization is idempotent even after restart.
        restart()
        for message in messages.values():
            first = _certify(stores[0], message)
            assert _certify(stores[1], message) == first
        assert all(stores[0].command(_effect_id(msg)) is None for msg in messages.values())
    finally:
        for store in stores:
            store.close()

    with PostgresPersistence(DSN, namespace=namespace) as reopened:
        view = _semantic_view(reopened)
        assert view["messages"] == view["applied"] == 8
        assert len(view["events"]) == len(view["receipts"]) == 8
        assert len(view["edges"]) == 6
        assert view["pending_effects"] == 0
        assert view["pending_commands"] == ()
        return view


def test_pg_two_equal_value_pc6_flows_converge_after_independent_worker_deaths(tmp_path):
    assert DSN is not None
    baseline = _execute("eq_base_" + uuid4().hex[:12], inject_faults=False)
    recovered = _execute("eq_fault_" + uuid4().hex[:12], inject_faults=True)
    # Persist a machine-readable result locally for CI evidence collection;
    # test logs and assertions remain authoritative if artifacts aren't uploaded.
    (tmp_path / "pc6-causal-equivalence.json").write_text(
        json.dumps({"baseline": baseline, "recovered": recovered},
                   default=str, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    assert recovered == baseline

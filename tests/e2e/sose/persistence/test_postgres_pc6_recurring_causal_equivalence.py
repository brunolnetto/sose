"""PC6 falsification: continuous vs recovered real-domain durable subgraph.

Cross-organization *total* interleaving is not a causal invariant. Compare
immutable per-effect certification, domain events, organizational clocks,
physical bookings and their causal ledger, not process-local worker identity.
The scope of this protocol is the Logistics dispatch/pickup/delivery subgraph.
"""
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4
import json
import os

import pytest

from sose.composition.boundary import BoundaryService
from sose.composition.model import BoundaryMessage
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.resource_intents import IntentResourceCoordinator
from sose.composition.scheduler import RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.logistics import simulation as logistics
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
ORGS = ("org-a", "org-b")
SLOT = timedelta(minutes=5)


def _canonical(items):
    return sorted(
        json.dumps(asdict(item), sort_keys=True, separators=(",", ":"), default=str)
        for item in items
    )


def _seed(ns):
    effects = {}
    shipments = {}
    sources = {}
    with PostgresPersistence(DSN, namespace=ns) as store:
        service = BoundaryService(store)
        for org in ORGS:
            entities = logistics.seed_reference(store, instance_key=org)
            shipments[org] = entities.shipment_id
            upstream = BoundaryMessage.create(
                contract_name="o2c.fulfillment_requested", contract_version=2,
                source_domain="order_to_cash", source_identity="order-" + org,
                destination_domain="warehouse_fulfillment",
                occurrence_key="fulfillment-requested", correlation_id=org,
                causation_id=None, produced_at=logistics.ORIGIN,
                payload={
                    "order_id": "order-" + org,
                    "fulfillment_order_id": "fulfillment-" + org,
                    "shipment_id": entities.shipment_id,
                },
            )
            service.publish(upstream)
            source = BoundaryMessage.create(
                contract_name="warehouse.dispatch_ready", contract_version=1,
                source_domain="warehouse_fulfillment",
                source_identity=entities.shipment_id,
                destination_domain="logistics", occurrence_key="dispatch-ready",
                correlation_id=org, causation_id=None, produced_at=logistics.ORIGIN,
                payload={
                    "shipment_id": entities.shipment_id,
                    "fulfillment_order_id": "fulfillment-" + org,
                },
            )
            service.publish(source)
            lease = service.claim_next(
                owner_id="setup-worker", now=logistics.ORIGIN,
                lease_duration=timedelta(hours=1), destination_domain="logistics",
                message_id=source.message_id,
            )
            assert lease is not None
            consumed = service.consume(
                lease=lease, registry=TradingCustomerRecoveryRunner._registry_for(source),
                now=logistics.ORIGIN,
            )
            effects[org] = consumed.consumer_effect_id
            sources[org] = source.message_id
    return effects, shipments, sources


def _schedule(store, org, owner):
    pool = ResourcePoolContract(
        resource_type="pickup_courier", pool_id="pc6-shared-dispatch",
        scope="shared", capacity=2,
    )
    return RecoverySchedule(
        runner=TradingCustomerRecoveryRunner(
            persistence=store, owner_id=owner, job_id="job-" + org,
            correlation_id=org, scoped_writer=True, max_actions=1,
            resource_policies={"composition.deliver_shipment": pool},
            resource_organizations={org: org},
            resource_slot_duration=SLOT, resource_retry_delay=SLOT,
        ),
        start_at=logistics.ORIGIN + (SLOT if org == "org-b" else timedelta()),
        interval=SLOT, max_slots=1,
    )


def _case(monkeypatch, *, interrupted):
    assert DSN
    ns = "pc6_equiv_" + uuid4().hex[:12]
    effects, shipments, sources = _seed(ns)
    first_slot = logistics.ORIGIN
    second_slot = first_slot + SLOT

    if interrupted:
        original_complete = IntentResourceCoordinator.complete
        killed = {"once": False}

        def interrupt_after_certification(self, effect_id):
            if effect_id == effects["org-a"] and not killed["once"]:
                killed["once"] = True
                with self._store.transaction(
                    owner_epoch=self._owner_epoch,
                    writer_scope=self._writer_scope,
                ) as uow:
                    assert uow.get_business_effect(effect_id) is not None
                    assert uow.get_command(effect_id) is None
                assert self._ledger.get(effect_id).status == "reserved"
                raise RuntimeError("injected worker death after real domain commit")
            return original_complete(self, effect_id)

        with monkeypatch.context() as patch:
            patch.setattr(
                IntentResourceCoordinator, "complete", interrupt_after_certification,
            )
            with PostgresPersistence(DSN, namespace=ns) as died:
                with pytest.raises(RuntimeError, match="worker death"):
                    _schedule(died, "org-a", "worker-a-before-death").run_due(now=first_slot)
                assert died.job_state("job-org-a").active_trigger_id is not None
            # Worker B is not required to wait for recovery of worker A.
            with PostgresPersistence(DSN, namespace=ns) as b:
                assert len(_schedule(b, "org-b", "worker-b").run_due(now=second_slot)) == 1
                assert b.temporal_resources().get(effects["org-a"]).status == "reserved"

        with PostgresPersistence(DSN, namespace=ns) as recovered:
            assert len(
                _schedule(recovered, "org-a", "worker-a-after-death").run_due(
                    now=second_slot,
                )
            ) == 1
    else:
        with PostgresPersistence(DSN, namespace=ns) as a:
            assert len(_schedule(a, "org-a", "worker-a").run_due(now=first_slot)) == 1
        with PostgresPersistence(DSN, namespace=ns) as b:
            assert len(_schedule(b, "org-b", "worker-b").run_due(now=second_slot)) == 1

    with PostgresPersistence(DSN, namespace=ns) as verified:
        ledger = verified.temporal_resources()
        assert ledger.audit()
        bookings = ledger.snapshot()
        domain_events = [
            event for event in verified.events()
            if event.entity_type == "shipment"
            and event.entity_id in set(shipments.values())
        ]
        job_clock = {}
        certified = []
        completed_shipments = []
        causal_links = []
        for org in ORGS:
            state = verified.job_state("job-" + org)
            assert state is not None
            assert state.active_trigger_id is None and state.run_count == 1
            job_clock[org] = (
                state.logical_time.isoformat(), state.next_tick,
                state.last_completed_trigger_id,
                tuple(asdict(x) for x in state.completed_batch_triggers),
            )
            receipt = verified.business_effect(effects[org])
            assert receipt is not None
            assert receipt.boundary_message_id == sources[org]
            assert receipt.correlation_id == org
            assert verified.command(effects[org]) is None
            certified.append(receipt)
            entity = verified.entity("shipment", shipments[org])
            assert entity is not None and entity.state == "delivered"
            completed_shipments.append(entity)
            booking = ledger.get(effects[org])
            assert booking is not None and booking.status == "released"
            assert booking.owner_id == org
            assert booking.causation_id == sources[org]
            causal_links.append((
                org, receipt.boundary_message_id, receipt.effect_id,
                booking.reservation_id, booking.causation_id,
            ))

        org_positions = {
            org: IntentResourceCoordinator(
                verified,
                {"composition.deliver_shipment": ResourcePoolContract(
                    resource_type="pickup_courier", pool_id="pc6-shared-dispatch",
                    scope="shared", capacity=2,
                )},
                slot_duration=SLOT, retry_delay=SLOT,
            ).logical_time(org).isoformat()
            for org in ORGS
        }
        return {
            "events": _canonical(domain_events),
            "entities": _canonical(completed_shipments),
            "certificates": _canonical(certified),
            "causal_links": sorted(causal_links),
            "ledger": bookings,
            "job_clocks": job_clock,
            "organization_positions": org_positions,
        }


def test_real_two_org_logistics_subgraph_is_causally_equivalent_after_recovery(monkeypatch):
    baseline = _case(monkeypatch, interrupted=False)
    restarted = _case(monkeypatch, interrupted=True)
    # Exactly compare attributed causal history, not the artificial
    # cross-organization total ordering of unrelated worker operations.
    assert restarted == baseline
    assert set(restarted["organization_positions"]) == set(ORGS)
    assert len(restarted["certificates"]) == 2
    assert len(restarted["causal_links"]) == 2

"""#545: real PostgreSQL source authenticity across fresh OS verifier processes.

This is a bounded test of signed native StateChart transitions and a public
UnitOfWork forgery; NOT a substitute for writer-role separation, owner-epoch
fencing, network/COMMIT ambiguity or composed W4b authorization.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from uuid import uuid4

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
OWNER = "warehouse:authenticated-source"
ISSUER = "engine:warehouse-authenticated"
KEY_ID = "issuer:v1"
SECRET = b"0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)


def _runtime(store: PostgresPersistence, authority: TransitionAuthority) -> Engine:
    registry = DomainRegistry()
    registry.register(EntityType("warehouse_fulfillment_order", FulfillmentOrderChart))
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=53),
        scheduler=Scheduler(),
    )
    return Engine(
        persistence=store, owner_id=OWNER, registry=registry,
        context=context, transition_authority=authority,
    )


def _verify_in_fresh_process(dsn, namespace, event_id, key, response):
    """A separate interpreter and PostgreSQL connection verify persisted truth."""
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            with store.transaction(owner_id=OWNER) as uow:
                source = uow.get_event(event_id)
                if source is None:
                    raise AssertionError("source event vanished after commit")
                root = TransitionAuthority(ISSUER, KEY_ID, key)
                receipt = root.verify(uow=uow, event=source)
        response.put(("verified", receipt.event_id))
    except ValueError as exc:
        response.put(("rejected", str(exc)))
    except BaseException as exc:
        response.put(("unexpected", repr(exc)))


def _fresh_verifier(dsn: str, namespace: str, event_id: str, key: bytes):
    ctx = get_context("spawn")
    response = ctx.Queue()
    process = ctx.Process(
        target=_verify_in_fresh_process,
        args=(dsn, namespace, event_id, key, response),
    )
    try:
        process.start()
        process.join(timeout=45)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
            raise AssertionError("PostgreSQL receipt verifier process hung")
        assert process.exitcode == 0, f"independent verifier crashed: {process.exitcode}"
        return response.get(timeout=10)
    finally:
        response.close()
        response.join_thread()
        process.close()


def test_pg_signed_warehouse_ship_is_replayable_but_public_forgery_is_not():
    """Full native ship path is signed; fake entity/event has no issuer receipt."""
    assert DSN is not None
    namespace = "trusted_receipt_" + uuid4().hex[:12]
    root = TransitionAuthority(ISSUER, KEY_ID, SECRET)
    order = FulfillmentOrder(
        id="order:signed", state="requested", created_at=NOW, updated_at=NOW,
    )
    untrusted = FulfillmentOrder(
        id="order:forged", state="requested", created_at=NOW, updated_at=NOW,
    )

    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)
            uow.save_entity(untrusted)

        runtime = _runtime(store, root)
        for step in ("allocate", "start_pick", "pack", "ship"):
            runtime.dispatch(Command(
                command_id=f"command:{step}", name=step,
                entity_type=order.entity_type, entity_id=order.id,
                owner_id=OWNER, due_at=NOW, issued_at=NOW,
                correlation_id="flow:signed-warehouse-order",
            ))
        signed_events = tuple(
            x for x in store.events()
            if x.name == "entity.state_transition" and x.entity_id == order.id
        )
        receipts = tuple(
            x for x in store.events()
            if x.name == ATTESTATION_EVENT and x.entity_id == order.id
        )
        assert len(signed_events) == len(receipts) == 4
        ship = next(x for x in signed_events if x.payload["trigger"] == "ship")
        assert ship.payload == {
            "trigger": "ship", "from_state": "packed", "to_state": "shipped",
        }
        assert store.entity(order.entity_type, order.id).state == "shipped"
        assert all(
            not (x.payload["trigger"] == "ship")
            for x in signed_events if x.event_id != ship.event_id
        )
        with store.transaction(owner_id=OWNER) as uow:
            assert root.verify(uow=uow, event=ship).causation_id == ship.event_id

        # A public writer reconstructs the expected deterministic source ID
        # and overwrites the mutable entity, but has no signing capability.
        forged = DomainEvent(
            event_id=deterministic_id(
                "event", OWNER, 0, untrusted.entity_type, untrusted.id,
                1, "ship", "packed", "shipped",
            ),
            name="entity.state_transition", entity_type=untrusted.entity_type,
            entity_id=untrusted.id, occurred_at=NOW, tick=0,
            owner_id=OWNER, causation_id="command:forged-ship",
            correlation_id="flow:forged-warehouse-order",
            payload={
                "trigger": "ship", "from_state": "packed", "to_state": "shipped",
            },
        )
        with store.transaction(owner_id=OWNER) as uow:
            fake_projection = uow.get_entity(untrusted.entity_type, untrusted.id)
            fake_projection.state = "shipped"
            fake_projection.version = 1
            fake_projection.updated_at = NOW
            uow.save_entity(fake_projection)
            uow.append_event(forged)
        assert store.entity(untrusted.entity_type, untrusted.id).state == "shipped"
        with store.transaction(owner_id=OWNER) as uow:
            with pytest.raises(ValueError, match="receipt missing"):
                root.verify(uow=uow, event=forged)

    # Three brand-new OS processes and PG connections. No in-memory runtime
    # object or cached StateChart can constitute the verifier's evidence.
    verified = _fresh_verifier(DSN, namespace, ship.event_id, SECRET)
    assert verified == ("verified", deterministic_id("engine-attested-transition", ship.event_id))
    bad_root = _fresh_verifier(DSN, namespace, ship.event_id, b"x" * 32)
    assert bad_root[0] == "rejected" and "signature invalid" in bad_root[1]
    forged_result = _fresh_verifier(DSN, namespace, forged.event_id, SECRET)
    assert forged_result[0] == "rejected" and "receipt missing" in forged_result[1]

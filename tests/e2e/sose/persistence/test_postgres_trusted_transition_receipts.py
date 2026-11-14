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
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.postgres import PostgresPersistence, StaleWriterError

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


@pytest.mark.parametrize("forged_version", (0, 3))
def test_pg_native_ship_rejects_untrusted_prestate_even_with_valid_issuer(
    forged_version,
):
    """#545: a real issuer cannot sign a successor of public-UoW forgery.

    Version zero at the wrong initial state and a plausible packed/version-3
    projection without native transition history must both fail BEFORE the
    actual Engine signs source and attestation into PostgreSQL.
    """
    assert DSN is not None
    namespace = "trusted_prestate_" + uuid4().hex[:12]
    authority = TransitionAuthority(ISSUER, KEY_ID, SECRET)
    order = FulfillmentOrder(
        id="order:public-prestate", state="requested",
        created_at=NOW, updated_at=NOW,
    )
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)
        with store.transaction(owner_id=OWNER) as uow:
            tampered = uow.get_entity(order.entity_type, order.id)
            tampered.state = "packed"
            tampered.version = forged_version
            tampered.updated_at = NOW
            uow.save_entity(tampered)
        prior_events = tuple(store.events())

        with pytest.raises(
            ValueError, match="unauthenticated prestate|trusted predecessor"
        ):
            _runtime(store, authority).dispatch(Command(
                command_id=f"command:forged-prestate:{forged_version}",
                name="ship", entity_type=order.entity_type,
                entity_id=order.id, owner_id=OWNER,
                due_at=NOW, issued_at=NOW,
                correlation_id="flow:forged-prestate",
            ))

        assert tuple(store.events()) == prior_events
        assert store.entity(order.entity_type, order.id).state == "packed"
        assert store.entity(order.entity_type, order.id).version == forged_version

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        assert recovered.entity(order.entity_type, order.id).state == "packed"
        assert recovered.entity(order.entity_type, order.id).version == forged_version
        assert not any(
            event.name == ATTESTATION_EVENT for event in recovered.events()
        )


def test_postgres_fenced_native_statechart_rejects_stale_issuer_after_takeover():
    """#545: stale issuer holding the correct HMAC key cannot durably sign.

    Two independent PostgreSQL connections share one namespace and owner.
    The DB-authoritative epoch, not self-declared event or issuer metadata,
    fences the displaced runtime before the StateChart/receipt can commit.
    A later lease legitimately continues the signed predecessor chain.
    This bounded case is not independent OS-worker/key-service certification.
    """
    assert DSN is not None
    namespace = "trusted_fence_" + uuid4().hex[:12]
    authority = TransitionAuthority(ISSUER, KEY_ID, SECRET)
    order = FulfillmentOrder(
        id="order:fenced-epoch", state="requested",
        created_at=NOW, updated_at=NOW,
    )
    with (
        PostgresPersistence(DSN, namespace=namespace) as older,
        PostgresPersistence(DSN, namespace=namespace) as newer,
    ):
        with older.transaction(owner_id=OWNER) as uow:
            uow.save_entity(order)
        first = older.claim_writer(OWNER, expected_epoch=older.writer_epoch())
        assert first.epoch == 1
        old_runtime = _runtime(FencedEnginePersistence(older, first), authority)
        old_runtime.dispatch(Command(
            command_id="command:fenced-initial-allocate", name="allocate",
            entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
            due_at=NOW, issued_at=NOW, correlation_id="flow:fenced-epoch",
        ))
        before_takeover = tuple(newer.events())
        assert len(before_takeover) == 2
        assert sum(e.name == ATTESTATION_EVENT for e in before_takeover) == 1

        second = newer.claim_writer(OWNER, expected_epoch=newer.writer_epoch())
        assert second.epoch == first.epoch + 1
        with pytest.raises(StaleWriterError, match="stale writer epoch"):
            old_runtime.dispatch(Command(
                command_id="command:stale-issuer-start-pick", name="start_pick",
                entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
                due_at=NOW, issued_at=NOW, correlation_id="flow:fenced-epoch",
            ))
        assert tuple(newer.events()) == before_takeover
        assert newer.entity(order.entity_type, order.id).state == "allocated"

        # Public UoW without an epoch has no authority after writer activation.
        with pytest.raises(StaleWriterError, match="owner_epoch is required"):
            with older.transaction(owner_id=OWNER) as uow:
                uow.get_entity(order.entity_type, order.id)

        promoted = FencedEnginePersistence(newer, second)
        _runtime(promoted, authority).dispatch(Command(
            command_id="command:new-issuer-start-pick", name="start_pick",
            entity_type=order.entity_type, entity_id=order.id, owner_id=OWNER,
            due_at=NOW, issued_at=NOW, correlation_id="flow:fenced-epoch",
        ))
        assert newer.entity(order.entity_type, order.id).state == "picking"
        events = tuple(newer.events())
        assert len(events) == 4
        assert sum(e.name == ATTESTATION_EVENT for e in events) == 2

    # Reopen a third DB connection and verify both immutable source receipts
    # under the active epoch. The old runtime's failed command is absent.
    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        restored = FencedEnginePersistence(recovered, second)
        with restored.transaction(owner_id=OWNER) as uow:
            signed = [e for e in uow.events() if e.name == "entity.state_transition"]
            assert [e.payload["trigger"] for e in signed] == ["allocate", "start_pick"]
            for event in signed:
                assert authority.verify(uow=uow, event=event).name == ATTESTATION_EVENT
        assert recovered.entity(order.entity_type, order.id).state == "picking"

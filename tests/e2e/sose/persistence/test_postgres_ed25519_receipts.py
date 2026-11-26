"""#494/#545: Ed25519-signed ship on real PostgreSQL, verified from the public key.

A fresh OS process and PostgreSQL connection receive only the issuer's public key.
They authenticate the Engine's signed ship and its full receipt chain, and they
refuse a receipt that a public writer forged with another Ed25519 key. The verifier
process cannot issue receipts itself.
"""
from __future__ import annotations

import os
from dataclasses import replace
from multiprocessing import get_context
from uuid import uuid4

import pytest

pytest.importorskip("cryptography")

from sose.core.ed25519_authority import (  # noqa: E402
    Ed25519TransitionAuthority,
    Ed25519TransitionVerifier,
)
from sose.core.events import Command  # noqa: E402
from sose.core.transition_authority import ATTESTATION_EVENT  # noqa: E402
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder  # noqa: E402
from sose.persistence.postgres import PostgresPersistence  # noqa: E402
from tests.e2e.sose.persistence.test_postgres_trusted_transition_receipts import (  # noqa: E402
    NOW,
    OWNER,
    _runtime,
)

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
ISSUER, KEY_ID = "engine:warehouse-ed25519", "ed25519:v1"
SIGNER = Ed25519TransitionAuthority(ISSUER, KEY_ID, bytes(range(32)))
CHAIN = ("entity.state_transition", ATTESTATION_EVENT)


def _verify_with_public_key(dsn, namespace, entity_id, public_key, response):
    """Only the public key crosses the process boundary."""
    try:
        verifier = Ed25519TransitionVerifier(ISSUER, KEY_ID, public_key)
        with PostgresPersistence(dsn, namespace=namespace) as store:
            events = [e for e in store.events() if e.entity_id == entity_id and e.name in CHAIN]
            entity = store.entity(events[0].entity_type, entity_id)
            head = verifier.verify_history(events=events, entity=entity)
        try:
            verifier._signed({"issuer_id": ISSUER, "key_id": KEY_ID})
        except PermissionError:
            response.put(("verified", head.event_id))
            return
        response.put(("unexpected", "a verifier issued a signature"))
    except ValueError as exc:
        response.put(("rejected", str(exc)))
    except BaseException as exc:
        response.put(("unexpected", repr(exc)))


def _fresh_verifier(namespace: str, entity_id: str, public_key: bytes):
    ctx = get_context("spawn")
    response = ctx.Queue()
    process = ctx.Process(target=_verify_with_public_key,
                          args=(DSN, namespace, entity_id, public_key, response))
    try:
        process.start()
        process.join(timeout=60)
        if process.is_alive():
            pytest.fail("public-key verifier process did not finish")
        return response.get(timeout=5)
    finally:
        if process.is_alive():
            process.kill()


def _ship(store, order_id: str, authority) -> None:
    with store.transaction(owner_id=OWNER) as uow:
        uow.save_entity(FulfillmentOrder(id=order_id, state="requested",
                                         created_at=NOW, updated_at=NOW))
    runtime = _runtime(store, authority)
    for step in ("allocate", "start_pick", "pack", "ship"):
        runtime.dispatch(Command(
            command_id=f"command:{order_id}:{step}", name=step,
            entity_type="warehouse_fulfillment_order", entity_id=order_id,
            owner_id=OWNER, due_at=NOW, issued_at=NOW, correlation_id=f"flow:{order_id}",
        ))


def test_pg_ed25519_ship_verifies_from_the_public_key_and_forgery_fails():
    assert DSN is not None
    namespace = "ed25519_receipt_" + uuid4().hex[:12]
    public_key = SIGNER.verifier().public_key
    impostor = Ed25519TransitionAuthority(ISSUER, KEY_ID, b"\x07" * 32)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        _ship(store, "order:signed", SIGNER)
        # A public writer with its own Ed25519 key and the right ids.
        _ship(store, "order:forged", impostor)
        receipts = [e for e in store.events()
                    if e.name == ATTESTATION_EVENT and e.entity_id == "order:signed"]
        assert len(receipts) == 4
        assert all(r.payload["algorithm"] == "ed25519" for r in receipts)

    assert _fresh_verifier(namespace, "order:signed", public_key)[0] == "verified"
    forged = _fresh_verifier(namespace, "order:forged", public_key)
    assert forged[0] == "rejected" and "signature invalid" in forged[1]

    # Rewriting a signed claim in the durable log is caught the same way.
    with PostgresPersistence(DSN, namespace=namespace) as store:
        receipt = next(e for e in store.events()
                       if e.name == ATTESTATION_EVENT and e.entity_id == "order:signed")
        tampered = replace(receipt, payload={**receipt.payload, "to_state": "shipped"})
        verifier = SIGNER.verifier()
        with pytest.raises(ValueError, match="signature invalid"):
            verifier.verify_history(
                events=[tampered if e.event_id == receipt.event_id else e
                        for e in store.events() if e.entity_id == "order:signed"
                        and e.name in CHAIN],
                entity=store.entity("warehouse_fulfillment_order", "order:signed"),
            )

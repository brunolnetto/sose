"""#494/#477: a lost COMMIT reply at each step of boundary publish → claim → ACK.

#477 asks for recovery "before publisher/worker response" along publication, claim and
ACK, "including crash-induced rollback and ACK retry/idempotence". Other PostgreSQL
tests kill workers between these steps (os._exit) or cut the transport. None of them
loses the reply of a *successful* COMMIT at the publish or claim step, and the
existing ACK case cuts the transport only after the reply has arrived.

A worker runs publish, claim_next and consume over the #584 wire proxy. The proxy is
armed inside one stage's write transaction, after its rows were written, so it
forwards that COMMIT, sees the backend's CommandComplete, and drops the reply. The
worker fails with a transport error. The parent reads the outcome on a fresh direct
connection and retries as a real caller would:

- **publish:** one message and one PENDING delivery. A retry returns the same
  delivery and adds nothing.
- **claim:** one CLAIMED delivery at epoch 1. Nobody else can claim it before the
  lease expires. After that, a new claim gets epoch 2, the old (epoch 1) lease is
  refused at ACK, and the new one ACKs.
- **ACK:** one consumption, the delivery CONSUMED and the Command staged once. A
  retry returns that consumption, and a counting handler shows it is not run again
  (the CONSUMED short-circuit answers before any lease check).

In every case the chain ends with exactly one consumption and one Command.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    BoundaryService,
    StaleBoundaryClaimError,
)
from sose.composition.model import BoundaryLease, BoundaryMessage, DeliveryStatus
from sose.core.events import Command
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_infrastructure_chaos import _free_port
from tests.e2e.sose.persistence.test_postgres_pc6_uncertain_effect_commit import (
    _start_commit_reply_drop_proxy,
)
from tests.support.chaos_waits import wait_for_marker_exists

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL TCP required")
NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
EFFECT = "command:boundary-effect"
DESTINATION = "logistics"


def message() -> BoundaryMessage:
    return BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse", source_identity="order-1",
        destination_domain=DESTINATION, occurrence_key="dispatch-1",
        correlation_id="lost-reply-order-1", causation_id=None, causation_kind=None,
        produced_at=NOW, payload={"order_id": "order-1"},
    )


def registry(calls: list | None = None) -> BoundaryConsumerRegistry:
    handlers = BoundaryConsumerRegistry()

    def stage_effect(_message, uow):
        if calls is not None:
            calls.append(_message.message_id)
        uow.save_command(Command(command_id=EFFECT, name="do_business",
                                 entity_type="shipment", entity_id="shipment-1", due_at=NOW))
        return EFFECT

    handlers.register(destination_domain=DESTINATION, contract_name="warehouse.dispatch_ready",
                      contract_version=1, handler=stage_effect)
    return handlers


def claim(service: BoundaryService, owner: str, now: datetime = NOW):
    return service.claim_next(owner_id=owner, now=now, lease_duration=LEASE,
                              destination_domain=DESTINATION)


# The worker arms the proxy only while the chosen stage runs, after its rows were
# written inside its transaction: the next COMMIT is that stage's own.
_WORKER = """
import sys
from pathlib import Path
from sose.composition.boundary import BoundaryService
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_boundary_lost_commit_reply import (
    NOW, claim, message, registry,
)

dsn, namespace, stage, arm = sys.argv[1:]
original = PostgresPersistence._apply_changes
active = [None]

def arming(self, before, after, dirty_records):
    result = original(self, before, after, dirty_records)
    if active[0] == stage and dirty_records and self.namespace == namespace:
        active[0] = "armed"
        Path(arm).write_text("armed_after_" + stage + "_rows")
    return result

PostgresPersistence._apply_changes = arming
with PostgresPersistence(dsn, namespace=namespace) as store:
    service = BoundaryService(store)
    active[0] = "publish"
    service.publish(message())
    active[0] = "claim" if active[0] != "armed" else "armed"
    lease = claim(service, "worker-a")
    active[0] = "consume" if active[0] != "armed" else "armed"
    service.consume(lease=lease, registry=registry(), now=NOW)
raise AssertionError("no COMMIT reply was lost")
"""


def _lose_reply_at(stage: str, namespace: str, tmp_path) -> None:
    proxy = worker = None
    try:
        proxy, proxied, arm, claimed, dropped = _start_commit_reply_drop_proxy(
            DSN, port=_free_port(), tmp_path=tmp_path,
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER, proxied, namespace, stage, str(arm)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_for_marker_exists(
            dropped, proxy, timeout=65,
            premature_exit_label=f"COMMIT proxy died before the {stage} reply",
            timeout_label=f"the {stage} COMMIT was never intercepted",
            termination_timeout=10,
        )
        assert dropped.read_text(encoding="utf-8") == "commit_complete_response_dropped"
        assert claimed.read_text(encoding="utf-8") == f"armed_after_{stage}_rows"
        assert not arm.exists()
        try:
            out, err = worker.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()
            out, err = worker.communicate(timeout=10)
            pytest.fail(f"the worker never observed the severed {stage} reply")
        assert worker.returncode != 0, ("an unacknowledged COMMIT was accepted", out, err)
        assert "OperationalError" in err, err[-3000:]
    finally:
        for process in (worker, proxy):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def _state(store: PostgresPersistence):
    messages, deliveries, consumptions = store.boundary_protocol_snapshot()
    with store.transaction() as uow:
        effects = [c for c in uow.commands() if c.command_id == EFFECT]
    return messages, deliveries, consumptions, effects


def _lease_from(delivery) -> BoundaryLease:
    """The lease the worker held but never saw confirmed, rebuilt from durable state."""
    return BoundaryLease(delivery_id=delivery.delivery_id, message_id=delivery.message_id,
                         owner_id=delivery.claim_owner_id, epoch=delivery.claim_epoch,
                         lease_expires_at=delivery.lease_expires_at)


def _assert_done_once(store: PostgresPersistence) -> None:
    messages, deliveries, consumptions, effects = _state(store)
    assert len(messages) == len(deliveries) == len(consumptions) == len(effects) == 1
    assert deliveries[0].status is DeliveryStatus.CONSUMED
    assert consumptions[0].consumer_effect_id == EFFECT


@pytest.mark.parametrize("stage", ["publish", "claim", "consume"])
def test_a_lost_commit_reply_at_each_boundary_step_recovers_exactly_once(stage, tmp_path):
    assert DSN is not None
    namespace = f"lost_{stage}_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace):
        pass                                               # create the namespace directly
    _lose_reply_at(stage, namespace, tmp_path)

    with PostgresPersistence(DSN, namespace=namespace) as store:
        service = BoundaryService(store)
        messages, deliveries, consumptions, effects = _state(store)
        assert len(messages) == len(deliveries) == 1, "the committed stage is durable once"
        delivery = deliveries[0]
        if stage == "publish":
            assert delivery.status is DeliveryStatus.PENDING and not consumptions and not effects
            assert service.publish(message()) == delivery      # retry adds nothing
            assert len(_state(store)[1]) == 1
            lease = claim(service, "worker-b")
            service.consume(lease=lease, registry=registry(), now=NOW)
        elif stage == "claim":
            assert delivery.status is DeliveryStatus.CLAIMED and delivery.claim_epoch == 1
            assert delivery.claim_owner_id == "worker-a" and not consumptions and not effects
            stale = _lease_from(delivery)
            assert claim(service, "worker-b") is None, "the unconfirmed lease still holds"
            later = NOW + LEASE + timedelta(seconds=1)
            fresh = claim(service, "worker-b", later)
            assert fresh is not None and fresh.epoch == 2
            with pytest.raises(StaleBoundaryClaimError):
                service.consume(lease=stale, registry=registry(), now=later)
            service.consume(lease=fresh, registry=registry(), now=later)
        else:
            assert delivery.status is DeliveryStatus.CONSUMED
            assert len(consumptions) == len(effects) == 1
            calls: list = []
            again = service.consume(lease=_lease_from(delivery), registry=registry(calls), now=NOW)
            assert again == consumptions[0], "the retry returns the committed ACK"
            assert calls == [], "the retry did not run the handler again"
        _assert_done_once(store)

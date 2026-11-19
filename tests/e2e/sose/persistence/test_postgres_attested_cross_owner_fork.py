"""#545: a second owner sharing the issuer cannot fork a rolled-back chain on PG."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")

OWNER_A = "warehouse:fork-owner-a"
OWNER_B = "warehouse:fork-owner-b"
NOW = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
AUTHORITY = TransitionAuthority("engine:shared-issuer", "issuer:v1", b"0" * 32)


def _runtime(store, owner):
    registry = DomainRegistry()
    registry.register(EntityType("warehouse_fulfillment_order", FulfillmentOrderChart))
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=5), scheduler=Scheduler(),
    )
    return Engine(persistence=store, owner_id=owner, registry=registry,
                  context=context, transition_authority=AUTHORITY)


def _command(step, order, owner):
    return Command(
        command_id=f"command:{owner}:{step}", name=step,
        entity_type=order.entity_type, entity_id=order.id, owner_id=owner,
        due_at=NOW, issued_at=NOW, correlation_id="flow:fork",
    )


def test_pg_other_owner_cannot_fork_history_after_projection_rollback():
    assert DSN is not None
    order = FulfillmentOrder(id="order:fork", state="requested", created_at=NOW, updated_at=NOW)
    with PostgresPersistence(DSN, namespace="attested_fork_" + uuid4().hex[:12]) as store:
        with store.transaction(owner_id=OWNER_A) as uow:
            uow.save_entity(order)
        runtime = _runtime(store, OWNER_A)
        for step in ("allocate", "start_pick", "pack"):
            runtime.dispatch(_command(step, order, OWNER_A))
        with store.transaction(owner_id=OWNER_A) as uow:
            uow.save_entity(order)  # entity-only writer rolls back to v0/requested
        recorded = tuple(store.events())
        with pytest.raises(ValueError, match="version-zero entity"):
            _runtime(store, OWNER_B).dispatch(_command("allocate", order, OWNER_B))
        assert tuple(store.events()) == recorded
        assert sum(e.name == ATTESTATION_EVENT for e in recorded) == 3
        assert store.entity(order.entity_type, order.id).version == 0

from __future__ import annotations

import os
from threading import Barrier, Thread
from uuid import uuid4

import pytest

from sose.domain.entity import Entity
from sose.domain.postgres import PostgresDomainWarehouse
from sose.domain.warehouse import DomainApplyResult, DomainMutation

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")


def test_postgres_domain_warehouse_serializes_same_entity_writers():
    assert DSN is not None
    namespace = f"domain_{uuid4().hex[:16]}"
    seed = PostgresDomainWarehouse(DSN, namespace=namespace)
    seed.apply(DomainMutation("v1", Entity(id="1", entity_type="order", state="queued", version=1)))
    seed.close()

    barrier = Barrier(2)
    results = []
    errors = []

    def worker(mid, version, state):
        warehouse = PostgresDomainWarehouse(DSN, namespace=namespace)
        try:
            barrier.wait(timeout=10)
            results.append(warehouse.apply(DomainMutation(
                mid, Entity(id="1", entity_type="order", state=state, version=version)
            )))
        except BaseException as exc:
            errors.append(exc)
        finally:
            warehouse.close()

    old = Thread(target=worker, args=("v2", 2, "running"))
    new = Thread(target=worker, args=("v3", 3, "closed"))
    old.start(); new.start(); old.join(timeout=20); new.join(timeout=20)
    assert errors == []
    reopened = PostgresDomainWarehouse(DSN, namespace=namespace)
    assert reopened.entity("order", "1").version == 3
    assert set(results) <= {DomainApplyResult.APPLIED, DomainApplyResult.SUPERSEDED}
    reopened.close()

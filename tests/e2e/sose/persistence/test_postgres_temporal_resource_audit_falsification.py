"""Tamper-resistant PostgreSQL resource causal audit.

Direct SQL corruptions are deliberate: the supported allocation API must
never generate them, but the auditor must detect them after a restart.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os

import pytest
from psycopg import sql

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import ResourceConflictError, TemporalOutage, TemporalReservation
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)
H = timedelta(hours=1)


def _new():
    return PostgresPersistence(DSN, namespace="resource_audit_" + uuid4().hex[:12])


def _pool(named=False):
    return ResourcePoolContract(
        resource_type="forklift" if named else "processor", pool_id="audit",
        scope="shared", capacity=2, instance_ids=("a", "b") if named else (),
    )


def _reserve(pool, rid, *, instance=None, units=1):
    return TemporalReservation(
        reservation_id=rid, address=pool.address(instance_id=instance),
        owner_id="audited-order", start_at=T0, end_at=T0 + 4*H, units=units,
    )


def _tamper(ledger, collection, operation, *params):
    table = {
        "events": ledger._events,
        "reservations": ledger._reservations,
        "outages": ledger._outages,
    }[collection]
    ledger._db.execute(sql.SQL(operation).format(table), params)


@pytest.mark.parametrize("case,match", [
    ("missing-cause", "missing causal predecessor"),
    ("missing-reservation", "lacks immutable allocation event"),
    ("missing-release", "release lacks durable causal event"),
    ("missing-outage", "outage lacks immutable causal event"),
    ("missing-recover", "recovery lacks durable causal event"),
    ("missing-failure", "terminated reservation lacks causal event"),
    ("overcapacity", "historical capacity overcommit"),
    ("duplicate-instance", "same physical instance double-allocated"),
    ("outage-overlap", "allocation overlaps resource outage"),
])
def test_pg_resource_audit_fails_closed_after_independent_database_corruption(case, match):
    assert DSN is not None
    named = case == "duplicate-instance"
    pool = _pool(named)
    with _new() as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        if named:
            ledger.reserve(_reserve(pool, "one", instance="a"))
            ledger.reserve(_reserve(pool, "two", instance="b"))
        elif case == "overcapacity":
            ledger.reserve(_reserve(pool, "one", units=1))
            ledger.reserve(_reserve(pool, "two", units=1))
        else:
            ledger.reserve(_reserve(pool, "one", units=1))
        if case == "missing-release":
            ledger.release("one", at=T0+H)
        if case == "missing-failure":
            ledger.fail(TemporalOutage(
                "failure", pool.address(), T0+H, T0+3*H,
            ))
        if case in ("missing-outage", "missing-recover"):
            outage = TemporalOutage(
                "outage", pool.address(), T0+4*H, T0+6*H,
            )
            ledger.fail(outage)
            if case == "missing-recover":
                ledger.recover("outage", at=T0+5*H)

        if case == "missing-cause":
            _tamper(ledger, "events", "UPDATE {} SET causation_event_id=%s WHERE event_id=%s",
                    "unknown-predecessor", "reserve:one")
        elif case == "missing-reservation":
            _tamper(ledger, "events", "DELETE FROM {} WHERE event_id=%s", "reserve:one")
        elif case == "missing-release":
            _tamper(ledger, "events", "DELETE FROM {} WHERE event_id=%s", "release:one")
        elif case == "missing-outage":
            _tamper(ledger, "events", "DELETE FROM {} WHERE event_id=%s", "outage:outage")
        elif case == "missing-recover":
            _tamper(ledger, "events", "DELETE FROM {} WHERE event_id=%s", "recover:outage")
        elif case == "missing-failure":
            _tamper(ledger, "events", "DELETE FROM {} WHERE event_id=%s", "failed:failure:one")
        elif case == "overcapacity":
            _tamper(ledger, "reservations",
                    "UPDATE {} SET units=2 WHERE reservation_id=%s", "two")
        elif case == "duplicate-instance":
            _tamper(ledger, "reservations",
                    "UPDATE {} SET instance_id=%s WHERE reservation_id=%s", "a", "two")
        elif case == "outage-overlap":
            # Outage directly inserted after a reservation without interrupting
            # it, mimicking an out-of-band writer bypassing the public API.
            pool_key = pool.address().lock_key()
            _tamper(ledger, "outages", """
                INSERT INTO {} (outage_id,pool_key,start_at,end_at)
                VALUES (%s,%s,%s,%s)
            """, "unreported", pool_key, T0+H, T0+2*H)
            _tamper(ledger, "events", """
                INSERT INTO {} (event_id,pool_key,subject_id,action,occurred_at)
                VALUES (%s,%s,%s,%s,%s)
            """, "outage:unreported", pool_key, "unreported", "failed", T0+H)
        with pytest.raises(ResourceConflictError, match=match):
            ledger.audit()

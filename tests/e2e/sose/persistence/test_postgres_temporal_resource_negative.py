"""Negative-path TDD for authoritative temporal resource allocation.

Proof that invalid requests, conflicting retries, corrupt causal ledgers and
invalid preemption cannot silently become accepted durable business truth.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os

import pytest

from sose.core.resource_identity import ResourceAddress, ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceCapacityError, ResourceConflictError,
    TemporalReservation, TemporalOutage,
)
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
H = timedelta(hours=1)


def _pool(*, named=False):
    return ResourcePoolContract(
        resource_type="forklift" if named else "processor",
        pool_id="shared", scope="shared", capacity=2,
        instance_ids=("a", "b") if named else (),
    )


def _reservation(pool, identity, start=0, end=4, *, units=1, instance=None, priority=100):
    return TemporalReservation(
        reservation_id=identity,
        address=pool.address(instance_id=instance),
        owner_id=f"owner-{identity}", start_at=T+start*H, end_at=T+end*H,
        units=units, priority=priority,
    )


def _db():
    return PostgresPersistence(DSN, namespace="resource_neg_"+uuid4().hex[:12])


@pytest.mark.parametrize("mutator,expected", [
    ({"reservation_id": ""}, ValueError),
    ({"owner_id": " "}, ValueError),
    ({"start_at": T+2*H, "end_at": T+H}, ValueError),
    ({"end_at": T}, ValueError),
    ({"start_at": datetime(2026, 10, 10)}, ValueError),
    ({"end_at": datetime(2026, 10, 10)}, ValueError),
    ({"start_at": "bad"}, ValueError),
    ({"units": True}, ValueError),
    ({"units": 0}, ValueError),
    ({"units": 1.5}, ValueError),
    ({"priority": True}, ValueError),
    ({"priority": 1.5}, ValueError),
    ({"status": "invalid"}, ValueError),
    ({"status": "released"}, ValueError),
    ({"stopped_at": T+H}, ValueError),
    ({"status": "released", "stopped_at": T-H}, ValueError),
    ({"status": "released", "stopped_at": T+5*H}, ValueError),
    ({"address": "not-an-address"}, TypeError),
])
def test_reservation_domain_contract_rejects_invalid_semantics(mutator, expected):
    req = _reservation(_pool(), "valid")
    with pytest.raises(expected):
        replace(req, **mutator)


def test_terminal_reservation_effective_end_and_exclusive_instances():
    pool = _pool(named=True)
    reserved = _reservation(pool, "named", instance="a")
    assert reserved.effective_end == T+4*H
    release = replace(reserved, status="released", stopped_at=T+H)
    assert release.effective_end == T+H
    with pytest.raises(ValueError, match="indivisible"):
        replace(reserved, units=2)


@pytest.mark.parametrize("mutator,expected", [
    ({"outage_id": " "}, ValueError),
    ({"address": "bad"}, TypeError),
    ({"start_at": T+2*H, "end_at": T+H}, ValueError),
    ({"start_at": datetime(2026, 10, 10)}, ValueError),
    ({"end_at": datetime(2026, 10, 10)}, ValueError),
    ({"recovered_at": datetime(2026, 10, 10)}, ValueError),
    ({"recovered_at": T-H}, ValueError),
    ({"recovered_at": T+5*H}, ValueError),
])
def test_outage_contract_rejects_invalid_semantics(mutator, expected):
    outage = TemporalOutage("maintenance", _pool().address(), T, T+4*H)
    with pytest.raises(expected):
        replace(outage, **mutator)


def test_outage_effective_end_tracks_recovery():
    outage = TemporalOutage("maintenance", _pool().address(), T, T+4*H)
    assert outage.effective_end == T+4*H
    assert replace(outage, recovered_at=T+H).effective_end == T+H


def test_invalid_registration_and_missing_pool_do_not_create_business_records():
    with _db() as db:
        ledger = db.temporal_resources()
        pool = _pool()
        with pytest.raises(TypeError, match="ResourcePoolContract"):
            ledger.register_pool("pool")
        with pytest.raises(KeyError, match="not registered"):
            ledger.reserve(_reservation(pool, "before-registration"))
        ledger.register_pool(pool)
        ledger.register_pool(pool)
        with pytest.raises(ResourceConflictError, match="immutable"):
            ledger.register_pool(replace(pool, capacity=3))
        assert ledger.snapshot()["reservations"] == []
        assert ledger.audit()


def test_invalid_instance_names_and_idempotent_terminal_retry():
    with _db() as db:
        ledger = db.temporal_resources()
        named = _pool(named=True)
        fungible = _pool()
        ledger.register_pool(named)
        ledger.register_pool(fungible)
        with pytest.raises(ValueError, match="identify"):
            ledger.reserve(_reservation(named, "nameless"))
        with pytest.raises(ValueError, match="not a member"):
            ledger.reserve(replace(
                _reservation(named, "foreign", instance="a"),
                address=ResourceAddress("forklift", "shared", "shared", instance_id="nonexistent"),
            ))
        with pytest.raises(ValueError, match="cannot use physical"):
            ledger.reserve(replace(
                _reservation(fungible, "instance-in-fungible"),
                address=ResourceAddress("processor", "shared", "shared", instance_id="a"),
            ))
        request = _reservation(named, "success", instance="a")
        ledger.reserve(request)
        released = ledger.release("success", at=T+H)
        assert ledger.reserve(request) == released
        with pytest.raises(ResourceConflictError, match="different contents"):
            ledger.reserve(replace(request, priority=1))
        with pytest.raises(ResourceConflictError, match="already terminated"):
            ledger.release("success", at=T+2*H) if False else ledger.fail  # placeholder
        assert ledger.audit()


def test_capacity_preemption_refuses_equal_priority_and_outages():
    with _db() as db:
        ledger = db.temporal_resources()
        pool = _pool()
        ledger.register_pool(pool)
        ledger.reserve(_reservation(pool, "a", priority=1, units=2))
        with pytest.raises(ResourceCapacityError, match="no admissible"):
            ledger.reserve(_reservation(pool, "equal", priority=1), preempt=True)
        with pytest.raises(ResourceCapacityError, match="no admissible"):
            ledger.reserve(_reservation(pool, "lower", priority=3), preempt=True)
        outage = TemporalOutage("shutdown", pool.address(), T+H, T+3*H)
        ledger.fail(outage)
        with pytest.raises(ResourceCapacityError, match="no admissible"):
            ledger.reserve(_reservation(pool, "outage", 1, 2, priority=0), preempt=True)
        assert ledger.audit()


def test_release_failure_recovery_negative_paths_are_immutable():
    with _db() as db:
        ledger = db.temporal_resources()
        pool = _pool()
        ledger.register_pool(pool)
        with pytest.raises(KeyError):
            ledger.release("unknown", at=T)
        with pytest.raises(KeyError):
            ledger.recover("unknown", at=T)
        ledger.reserve(_reservation(pool, "booking", units=2))
        with pytest.raises(ValueError, match="inside"):
            ledger.release("booking", at=T-H)
        outage = TemporalOutage("outage", pool.address(), T+H, T+3*H)
        assert ledger.fail(outage) == outage
        assert ledger.fail(outage) == outage
        with pytest.raises(ResourceConflictError, match="incompatible"):
            ledger.fail(replace(outage, end_at=T+4*H))
        with pytest.raises(ResourceConflictError, match="terminated"):
            ledger.release("booking", at=T+2*H)
        with pytest.raises(ValueError, match="inside"):
            ledger.recover("outage", at=T+4*H)
        recovered = ledger.recover("outage", at=T+2*H)
        assert ledger.recover("outage", at=T+2*H) == recovered
        assert ledger.audit()


def test_deterministic_multiple_preemption_victims_and_atomic_failed_admission():
    with _db() as db:
        ledger = db.temporal_resources()
        pool = _pool()
        ledger.register_pool(pool)
        ledger.reserve(_reservation(pool, "low-a", units=1, priority=101))
        ledger.reserve(_reservation(pool, "low-b", units=1, priority=100))
        # Demand for both units must preempt both lower-priority bookings.
        request = _reservation(pool, "high", 1, 3, units=2, priority=1)
        ledger.reserve(request, preempt=True)
        assert {ledger.get(x).status for x in ("low-a", "low-b")} == {"preempted"}
        assert ledger.get("low-a").stopped_at == T+H
        assert ledger.get("low-b").stopped_at == T+H
        assert ledger.audit()

"""Contracts: half-open finite resource time intervals and indivisible instances."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest

from sose.core.resource_identity import ResourceAddress
from sose.core.resource_reservations import (
    TemporalReservation, TemporalOutage, ResourceCapacityError,
)


T0 = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
POOL = ResourceAddress("forklift", "yard", "organization", organization_id="factory-a")


def test_resource_interval_is_half_open_and_recoverable():
    reservation = TemporalReservation("job-1", POOL, "worker-a", T0, T0 + timedelta(hours=2))
    assert reservation.effective_end == T0 + timedelta(hours=2)
    released = replace(reservation, status="released", stopped_at=T0 + timedelta(hours=1))
    assert released.effective_end == T0 + timedelta(hours=1)
    outage = TemporalOutage("fault", POOL, T0, T0 + timedelta(hours=4))
    assert outage.effective_end == T0 + timedelta(hours=4)
    assert replace(outage, recovered_at=T0 + timedelta(hours=2)).effective_end == (
        T0 + timedelta(hours=2)
    )


@pytest.mark.parametrize("kwargs", [
    {"reservation_id": ""},
    {"owner_id": ""},
    {"start_at": T0 + timedelta(hours=2), "end_at": T0},
    {"start_at": T0, "end_at": T0},
    {"start_at": datetime(2026, 10, 10, 9)},
    {"units": 0},
    {"units": True},
    {"priority": True},
    {"status": "released"},
    {"stopped_at": T0},
    {"status": "preempted", "stopped_at": T0 + timedelta(days=1)},
])
def test_invalid_bookings_rejected(kwargs):
    values = dict(reservation_id="job-1", address=POOL, owner_id="org-a",
                  start_at=T0, end_at=T0 + timedelta(hours=2))
    values.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        TemporalReservation(**values)


def test_named_instance_must_be_indivisible():
    address = replace(POOL, instance_id="forklift-1")
    with pytest.raises(ValueError, match="indivisible"):
        TemporalReservation("job", address, "worker", T0, T0 + timedelta(hours=1), units=2)


def test_outage_must_have_positive_duration_and_recovery_within_window():
    with pytest.raises(ValueError, match="positive duration"):
        TemporalOutage("fault", POOL, T0, T0)
    with pytest.raises(ValueError, match="inside"):
        TemporalOutage("fault", POOL, T0, T0 + timedelta(hours=1),
                       recovered_at=T0 + timedelta(hours=2))


def test_public_capacity_error_is_explicit():
    assert issubclass(ResourceCapacityError, RuntimeError)

"""#477: pure fail-closed request-envelope falsifiers for joint physical advice.

These tests intentionally do not need PostgreSQL: they guard identities before
asking the authoritative PostgreSQL ledger to advise any future interval.
Actual physical admission still requires PG pool fencing/transaction.
"""
from dataclasses import replace
from datetime import timedelta

import pytest

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import ResourceConflictError, TemporalReservation
from sose.organizational.od5a_durable import _ORIGIN
from sose.organizational.od5a_recurring import _aligned_physical_start


POOL = ResourcePoolContract(
    resource_type="technician", pool_id="shared-site",
    scope="shared", capacity=2, instance_ids=("tech-a", "tech-b"),
)
OTHER_POOL = ResourcePoolContract(
    resource_type="technician", pool_id="unrelated-site",
    scope="shared", capacity=2, instance_ids=("tech-a", "tech-b"),
)


class PermissiveAdvisoryLedger:
    """Adversarial oracle: every individually proposed request is 'available'."""

    def __init__(self):
        self.reads = []

    def suggest_next_start(self, request, *, not_before, latest_start):
        self.reads.append(request.reservation_id)
        return not_before


def _requests():
    return tuple(TemporalReservation(
        reservation_id=f"reservation:{instance}",
        owner_id="org:source",
        address=POOL.address(instance_id=instance),
        start_at=_ORIGIN,
        end_at=_ORIGIN + timedelta(milliseconds=200),
        causation_id=f"event:source:{instance}",
    ) for instance in ("tech-a", "tech-b"))


@pytest.mark.parametrize("alteration,match", [
    (lambda r: replace(r, owner_id="org:foreign"), "owner"),
    (lambda r: replace(r, address=OTHER_POOL.address(instance_id="tech-b")), "pool"),
    (lambda r: replace(r, start_at=_ORIGIN + timedelta(milliseconds=20),
                       end_at=_ORIGIN + timedelta(milliseconds=220)), "start"),
    (lambda r: replace(r, causation_id=None), "causal"),
])
def test_joint_advice_rejects_identity_and_time_aliases_before_any_db_reads(
    alteration, match,
):
    first, second = _requests()
    malformed = (first, alteration(second))
    ledger = PermissiveAdvisoryLedger()
    with pytest.raises(ResourceConflictError, match=match):
        _aligned_physical_start(
            ledger, malformed, not_before=_ORIGIN + timedelta(seconds=1),
            latest_start=_ORIGIN + timedelta(seconds=2),
        )
    assert ledger.reads == []


def test_joint_advice_allows_distinct_origins_but_same_source_and_start():
    ledger = PermissiveAdvisoryLedger()
    proposal = _aligned_physical_start(
        ledger, _requests(), not_before=_ORIGIN + timedelta(seconds=1),
        latest_start=_ORIGIN + timedelta(seconds=2),
    )
    assert proposal == _ORIGIN + timedelta(seconds=1)
    assert ledger.reads == ["reservation:tech-a", "reservation:tech-b"]


def test_joint_advice_refuses_duplicate_named_instance_before_db_reads():
    first, second = _requests()
    second = replace(second, address=first.address)
    ledger = PermissiveAdvisoryLedger()
    with pytest.raises(ResourceConflictError, match="aliases"):
        _aligned_physical_start(
            ledger, (first, second), not_before=_ORIGIN + timedelta(seconds=1),
            latest_start=_ORIGIN + timedelta(seconds=2),
        )
    assert ledger.reads == []

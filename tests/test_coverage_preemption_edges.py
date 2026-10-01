from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sose.backends.base import ResourceLease, ResourcePreemption
from sose.core.preemption import DurablePreemptiveResourceManager
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
    ResourcePreemptionResult,
)
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _definition(name="crew", capacity=1):
    return PreemptiveResourceDefinition(name, capacity)


def _demand(
    request_id="request",
    resource_name="crew",
    *,
    priority=100,
    preempt=False,
    sequence=1,
):
    return PreemptiveResourceDemand(
        request_id=request_id,
        resource_name=resource_name,
        priority=priority,
        preempt=preempt,
        requested_at=NOW,
        sequence=sequence,
    )


def _reservation(
    request_id="holder",
    resource_name="crew",
    *,
    reservation_id="res-holder",
    priority=100,
    sequence=1,
):
    return PreemptiveResourceReservation(
        reservation_id=reservation_id,
        request_id=request_id,
        resource_name=resource_name,
        acquired_at=NOW,
        priority=priority,
        sequence=sequence,
    )


def _release(
    reservation_id="res-holder",
    resource_name="crew",
    *,
    intent_id="release-holder",
):
    return PreemptiveResourceReleaseIntent(
        intent_id=intent_id,
        reservation_id=reservation_id,
        resource_name=resource_name,
        requested_at=NOW,
    )


def _preemption_result(resource_name="crew", *, result_id="result"):
    return ResourcePreemptionResult(
        result_id=result_id,
        resource_name=resource_name,
        displaced_reservation_id="res-holder",
        displaced_request_id="holder",
        preempting_request_id="urgent",
        successor_reservation_id="res-urgent",
        preempted_at=NOW,
        sequence=2,
    )


class _SnapshotPersistence:
    def __init__(
        self,
        *,
        definitions=(),
        reservations=(),
        demands=(),
        releases=(),
        results=(),
    ):
        self.definitions = tuple(definitions)
        self.reservations = tuple(reservations)
        self.demands = tuple(demands)
        self.releases = tuple(releases)
        self.results = tuple(results)

    def preemptive_resource_definitions(self):
        return self.definitions

    def preemptive_resource_reservations(self):
        return self.reservations

    def preemptive_resource_demands(self):
        return self.demands

    def preemptive_resource_release_intents(self):
        return self.releases

    def resource_preemption_results(self):
        return self.results


@pytest.mark.parametrize(
    ("persistence", "message"),
    [
        (
            _SnapshotPersistence(reservations=(_reservation(resource_name="missing"),)),
            "reservation references unknown definition",
        ),
        (
            _SnapshotPersistence(demands=(_demand(resource_name="missing"),)),
            "demand references unknown definition",
        ),
        (
            _SnapshotPersistence(
                definitions=(_definition(),),
                releases=(_release(),),
            ),
            "release references unknown reservation",
        ),
        (
            _SnapshotPersistence(
                definitions=(_definition(), _definition("other")),
                reservations=(_reservation(),),
                releases=(_release(resource_name="other"),),
            ),
            "release targets wrong resource",
        ),
        (
            _SnapshotPersistence(
                definitions=(_definition(capacity=1),),
                reservations=(
                    _reservation(request_id="one", reservation_id="res-one"),
                    _reservation(
                        request_id="two",
                        reservation_id="res-two",
                        sequence=2,
                    ),
                ),
            ),
            "exceeds durable capacity",
        ),
        (
            _SnapshotPersistence(
                definitions=(_definition(),),
                reservations=(_reservation(request_id="same"),),
                demands=(_demand(request_id="same"),),
            ),
            "both pending and reserved",
        ),
        (
            _SnapshotPersistence(
                results=(_preemption_result(resource_name="missing"),),
            ),
            "preemption result references unknown definition",
        ),
    ],
)
def test_preemptive_rebuild_rejects_corrupt_durable_truth(persistence, message):
    with pytest.raises(RuntimeError, match=message):
        DurablePreemptiveResourceManager(persistence).validate_rebuild()


class _CancelBackend:
    def __init__(self, result: bool):
        self.result = result

    def cancel_preemptive_resource_request(self, request_id):
        return self.result


class _CancelUow:
    def __init__(self, demand):
        self.demand = demand
        self.deleted = []

    def get_preemptive_resource_demand(self, request_id):
        return self.demand

    def delete_preemptive_resource_demand(self, request_id):
        self.deleted.append(request_id)


class _CancelPersistence(_SnapshotPersistence):
    def __init__(self, *, demand, transaction_demand):
        super().__init__(
            definitions=(_definition(),),
            demands=(demand,),
        )
        self.uow = _CancelUow(transaction_demand)

    @contextmanager
    def transaction(self):
        yield self.uow


def test_preemptive_request_rejects_unknown_resource_and_duplicate_id():
    persistence = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(persistence)

    with pytest.raises(KeyError, match="unknown preemptive resource definition"):
        manager.request(
            SimpleNamespace(),
            resource_name="missing",
            request_id="request",
            requested_at=NOW,
        )

    manager.define(_definition())
    with persistence.transaction() as uow:
        uow.save_preemptive_resource_demand(_demand())

    with pytest.raises(ValueError, match="request already exists"):
        manager.request(
            SimpleNamespace(),
            resource_name="crew",
            request_id="request",
            requested_at=NOW,
        )


def test_cancel_pending_preserves_demand_when_backend_refuses_cancellation():
    demand = _demand()
    persistence = _SnapshotPersistence(
        definitions=(_definition(),),
        demands=(demand,),
    )
    manager = DurablePreemptiveResourceManager(persistence)

    assert manager.cancel_pending(_CancelBackend(False), demand.request_id) is False


def test_cancel_pending_rejects_transaction_change():
    demand = _demand()
    changed = _demand(priority=1)
    persistence = _CancelPersistence(
        demand=demand,
        transaction_demand=changed,
    )
    manager = DurablePreemptiveResourceManager(persistence)

    with pytest.raises(RuntimeError, match="changed during cancellation"):
        manager.cancel_pending(_CancelBackend(True), demand.request_id)


class _ReleaseUow:
    def __init__(self, *, reservation=None, intent=None):
        self.reservation = reservation
        self.intent = intent
        self.saved_intents = []
        self.deleted_reservations = []
        self.deleted_intents = []

    def get_preemptive_resource_reservation(self, reservation_id):
        return self.reservation

    def save_preemptive_resource_release_intent(self, intent):
        self.saved_intents.append(intent)

    def get_preemptive_resource_release_intent(self, intent_id):
        return self.intent

    def delete_preemptive_resource_reservation(self, reservation_id):
        self.deleted_reservations.append(reservation_id)

    def delete_preemptive_resource_release_intent(self, intent_id):
        self.deleted_intents.append(intent_id)


class _ReleasePersistence(_SnapshotPersistence):
    def __init__(self, *, reservation, transaction_reservation=None, intent=None):
        super().__init__(
            definitions=(_definition(),),
            reservations=(() if reservation is None else (reservation,)),
        )
        self.uow = _ReleaseUow(
            reservation=transaction_reservation,
            intent=intent,
        )

    @contextmanager
    def transaction(self):
        yield self.uow


def test_release_returns_false_for_unknown_reservation():
    manager = DurablePreemptiveResourceManager(
        _SnapshotPersistence(definitions=(_definition(),))
    )
    assert manager.release(SimpleNamespace(), "missing") is False


def test_release_requires_reconstructed_backend_lease():
    reservation = _reservation()
    manager = DurablePreemptiveResourceManager(
        _SnapshotPersistence(
            definitions=(_definition(),),
            reservations=(reservation,),
        )
    )

    with pytest.raises(RuntimeError, match="backend lease is not reconstructed"):
        manager.release(SimpleNamespace(), reservation.reservation_id)


def test_release_loses_race_when_reservation_changes_in_transaction():
    reservation = _reservation()
    changed = _reservation(priority=1)
    persistence = _ReleasePersistence(
        reservation=reservation,
        transaction_reservation=changed,
    )
    manager = DurablePreemptiveResourceManager(persistence)
    manager._backend_leases[reservation.reservation_id] = ResourceLease(
        lease_id="lease-holder",
        request_id=reservation.request_id,
        resource_name=reservation.resource_name,
        acquired_at=NOW,
    )

    assert manager.release(SimpleNamespace(now=NOW), reservation.reservation_id) is False
    assert persistence.uow.saved_intents == []


def test_record_acquired_ignores_stale_callback_without_durable_demand():
    manager = DurablePreemptiveResourceManager(
        _SnapshotPersistence(definitions=(_definition(),))
    )
    manager._record_acquired(
        request_id="gone",
        lease=ResourceLease("lease-gone", "gone", "crew", NOW),
    )
    assert manager._pending_grants == {}


def test_record_preempted_requires_preemptor_and_valid_displaced_resource():
    reservation = _reservation()
    persistence = _SnapshotPersistence(
        definitions=(_definition(), _definition("other")),
        reservations=(reservation,),
    )
    manager = DurablePreemptiveResourceManager(persistence)

    with pytest.raises(RuntimeError, match="missing preempting request identity"):
        manager._record_preempted(
            ResourcePreemption(
                lease_id="lease-holder",
                request_id="holder",
                resource_name="crew",
                preempted_at=NOW,
                preempted_by=None,
            )
        )

    stale = DurablePreemptiveResourceManager(
        _SnapshotPersistence(definitions=(_definition(),))
    )
    stale._record_preempted(
        ResourcePreemption(
            lease_id="lease-gone",
            request_id="gone",
            resource_name="crew",
            preempted_at=NOW,
            preempted_by="urgent",
        )
    )
    assert stale._pending_preemptions == {}

    with pytest.raises(RuntimeError, match="targets wrong resource"):
        manager._record_preempted(
            ResourcePreemption(
                lease_id="lease-holder",
                request_id="holder",
                resource_name="other",
                preempted_at=NOW,
                preempted_by="urgent",
            )
        )


def _seed_preemption_handshake(
    *,
    demand=None,
    displaced=None,
    lease=None,
    event_resource="crew",
):
    persistence = _SnapshotPersistence(
        definitions=(_definition(), _definition("other")),
        demands=(() if demand is None else (demand,)),
        reservations=(() if displaced is None else (displaced,)),
    )
    manager = DurablePreemptiveResourceManager(persistence)
    if lease is not None:
        manager._pending_grants["urgent"] = lease
    manager._pending_preemptions["urgent"] = ResourcePreemption(
        lease_id="lease-holder",
        request_id="holder",
        resource_name=event_resource,
        preempted_at=NOW,
        preempted_by="urgent",
    )
    return persistence, manager


def test_try_commit_preemption_handles_incomplete_and_cross_resource_pairs():
    lease = ResourceLease("lease-urgent", "urgent", "crew", NOW)

    _, manager = _seed_preemption_handshake(
        demand=None,
        displaced=_reservation(),
        lease=lease,
    )
    assert manager._try_commit_preemption("urgent") is False

    cross_demand = _demand(
        request_id="urgent",
        resource_name="other",
        priority=1,
        preempt=True,
        sequence=2,
    )
    _, manager = _seed_preemption_handshake(
        demand=cross_demand,
        displaced=_reservation(),
        lease=lease,
    )
    with pytest.raises(RuntimeError, match="pair crosses resources"):
        manager._try_commit_preemption("urgent")


class _PreemptionUow:
    def __init__(self, *, demand=None, reservation=None):
        self.demand = demand
        self.reservation = reservation

    def get_preemptive_resource_demand(self, request_id):
        return self.demand

    def get_preemptive_resource_reservation(self, reservation_id):
        return self.reservation


class _PreemptionRacePersistence(_SnapshotPersistence):
    def __init__(
        self,
        *,
        demand,
        displaced,
        transaction_demand=None,
        transaction_reservation=None,
    ):
        super().__init__(
            definitions=(_definition(),),
            demands=(demand,),
            reservations=(displaced,),
        )
        self.uow = _PreemptionUow(
            demand=transaction_demand,
            reservation=transaction_reservation,
        )

    @contextmanager
    def transaction(self):
        yield self.uow


@pytest.mark.parametrize("change", ["demand", "reservation"])
def test_try_commit_preemption_loses_transaction_races(change):
    demand = _demand(
        request_id="urgent",
        priority=1,
        preempt=True,
        sequence=2,
    )
    displaced = _reservation()
    persistence = _PreemptionRacePersistence(
        demand=demand,
        displaced=displaced,
        transaction_demand=(None if change == "demand" else demand),
        transaction_reservation=(displaced if change == "demand" else None),
    )
    manager = DurablePreemptiveResourceManager(persistence)
    manager._pending_grants["urgent"] = ResourceLease(
        "lease-urgent", "urgent", "crew", NOW
    )
    manager._pending_preemptions["urgent"] = ResourcePreemption(
        "lease-holder", "holder", "crew", NOW, "urgent"
    )

    assert manager._try_commit_preemption("urgent") is False


def test_try_commit_normal_grant_requires_both_lease_and_demand():
    manager = DurablePreemptiveResourceManager(
        _SnapshotPersistence(definitions=(_definition(),))
    )
    assert manager._try_commit_normal_grant("missing") is False

    manager._pending_grants["lease-only"] = ResourceLease(
        "lease-only", "lease-only", "crew", NOW
    )
    assert manager._try_commit_normal_grant("lease-only") is False


class _NormalGrantUow:
    def __init__(self, demand):
        self.demand = demand

    def get_preemptive_resource_demand(self, request_id):
        return self.demand


class _NormalGrantRacePersistence(_SnapshotPersistence):
    def __init__(
        self,
        *,
        demand,
        transaction_demand,
        existing=(),
        releases=(),
    ):
        super().__init__(
            definitions=(_definition(),),
            demands=(demand,),
            reservations=tuple(existing),
            releases=tuple(releases),
        )
        self.uow = _NormalGrantUow(transaction_demand)

    @contextmanager
    def transaction(self):
        yield self.uow


def test_normal_grant_waits_for_release_or_full_durable_capacity():
    demand = _demand()
    lease = ResourceLease("lease-request", "request", "crew", NOW)

    releasing_persistence = _SnapshotPersistence(
        definitions=(_definition(),),
        demands=(demand,),
        releases=(_release(),),
    )
    manager = DurablePreemptiveResourceManager(releasing_persistence)
    manager._pending_grants[demand.request_id] = lease
    assert manager._try_commit_normal_grant(demand.request_id) is False

    full_persistence = _SnapshotPersistence(
        definitions=(_definition(),),
        demands=(demand,),
        reservations=(_reservation(),),
    )
    manager = DurablePreemptiveResourceManager(full_persistence)
    manager._pending_grants[demand.request_id] = lease
    assert manager._try_commit_normal_grant(demand.request_id) is False


def test_commit_normal_grant_returns_concurrent_reservation_or_rejects_change():
    demand = _demand()
    lease = ResourceLease("lease-request", "request", "crew", NOW)
    existing = _reservation(
        request_id="request",
        reservation_id="res-request",
    )

    persistence = _NormalGrantRacePersistence(
        demand=demand,
        transaction_demand=None,
        existing=(existing,),
    )
    assert DurablePreemptiveResourceManager(persistence)._commit_normal_grant(
        demand, lease
    ) == existing

    persistence = _NormalGrantRacePersistence(
        demand=demand,
        transaction_demand=None,
    )
    with pytest.raises(RuntimeError, match="demand changed before grant"):
        DurablePreemptiveResourceManager(persistence)._commit_normal_grant(
            demand, lease
        )


def test_reconcile_pending_grants_skips_other_resources_and_preemption_success(monkeypatch):
    manager = DurablePreemptiveResourceManager(
        _SnapshotPersistence(definitions=(_definition(), _definition("other")))
    )
    manager._pending_grants = {
        "other": ResourceLease("lease-other", "other", "other", NOW),
        "preempt": ResourceLease("lease-preempt", "preempt", "crew", NOW),
        "normal": ResourceLease("lease-normal", "normal", "crew", NOW),
    }
    calls = []

    monkeypatch.setattr(
        manager,
        "_try_commit_preemption",
        lambda request_id: calls.append(("preempt", request_id))
        or request_id == "preempt",
    )
    monkeypatch.setattr(
        manager,
        "_try_commit_normal_grant",
        lambda request_id: calls.append(("normal", request_id)) or True,
    )

    manager._reconcile_pending_grants("crew")

    assert ("preempt", "other") not in calls
    assert ("preempt", "preempt") in calls
    assert ("normal", "preempt") not in calls
    assert ("normal", "normal") in calls


@pytest.mark.parametrize("changed", ["intent", "reservation"])
def test_finalize_release_rejects_transaction_changes(changed):
    reservation = _reservation()
    intent = _release()
    persistence = _ReleasePersistence(
        reservation=reservation,
        transaction_reservation=(
            _reservation(priority=1) if changed == "reservation" else reservation
        ),
        intent=(_release(intent_id="changed") if changed == "intent" else intent),
    )
    manager = DurablePreemptiveResourceManager(persistence)

    with pytest.raises(RuntimeError, match="changed"):
        manager._finalize_release_intent(intent, reservation)


class _InterruptedReleasePersistence(_SnapshotPersistence):
    def __init__(self, intent):
        super().__init__(
            definitions=(_definition(),),
            releases=(intent,),
        )
        self.uow = _ReleaseUow()

    @contextmanager
    def transaction(self):
        yield self.uow


def test_finalize_interrupted_release_removes_stale_intent_without_reservation():
    intent = _release()
    persistence = _InterruptedReleasePersistence(intent)
    DurablePreemptiveResourceManager(persistence)._finalize_interrupted_releases()

    assert persistence.uow.deleted_intents == [intent.intent_id]


def test_definition_rejects_unknown_resource():
    manager = DurablePreemptiveResourceManager(_SnapshotPersistence())

    with pytest.raises(KeyError, match="unknown preemptive resource definition"):
        manager._definition("missing")

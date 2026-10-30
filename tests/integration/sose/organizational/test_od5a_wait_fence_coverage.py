"""#590: adversarial OD-5a native physical preview and wait/fence invariants.

Reference unit boundaries only: an advisory/synthetic preview is never
evidence of a PostgreSQL booking or a successfully completed historical run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.core.identity import deterministic_id
from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import ResourceConflictError
from sose.core.runtime import ResourceReservation
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.organizational import od5a_recurring as recurring
from sose.persistence.memory import MemoryPersistence


AT = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
JOB = "org:od5a-wait-coverage"
OWNER = "worker:coverage"


def _physical(reservation_id: str) -> ResourceReservation:
    return ResourceReservation(
        reservation_id=reservation_id,
        request_id=f"request:{reservation_id}",
        resource_name=f"{JOB}/execution:0",
        acquired_at=AT,
    )


def _lease(*, state: str, attributes: dict, at: datetime = AT) -> Entity:
    return Entity(
        id=JOB, entity_type="od5a_runner_lease",
        state=state, created_at=at, updated_at=at,
        attributes=attributes,
    )


def _job(*, completed: bool) -> SimulationJobState:
    count = 1 if completed else 0
    trigger = deterministic_id("od5a-recurring-tick", JOB, count or 1)
    return SimulationJobState(
        job_id=JOB, domain_name="od5a", config_json="{}",
        config_revision=1, status="ready" if completed else "running",
        initialized=True, logical_time=AT,
        next_tick=count, run_count=count, bootstrap_state={},
        last_completed_trigger_id=trigger if completed else None,
        active_trigger_id=None if completed else trigger,
        phase="idle" if completed else "advance",
    )


def test_speculative_preview_tracks_exact_grant_and_release_without_source_events():
    original = _physical("old")
    fresh = _physical("new")
    preview = recurring._PhysicalStepPreview((original,))
    assert preview.resource_reservations() == (original,)
    assert preview.get_resource_reservation("absent") is None
    preview.save_resource_reservation(fresh)
    assert preview.get_resource_reservation("new") == fresh
    assert preview.grants == [fresh]
    preview.append_event(object())
    preview.delete_resource_reservation("old")
    assert preview.releases == [original]
    assert preview.get_resource_reservation("old") is None
    assert preview.resource_reservations() == (fresh,)


def test_speculative_preview_refuses_duplicate_physical_grant_without_mutation():
    existing = _physical("same")
    preview = recurring._PhysicalStepPreview((existing,))
    with pytest.raises(RuntimeError, match="duplicate physical reservation"):
        preview.save_resource_reservation(existing)
    assert preview.resource_reservations() == (existing,)
    assert preview.grants == []
    assert preview.releases == []


def test_speculative_preview_refuses_unowned_release_without_mutation():
    existing = _physical("original")
    preview = recurring._PhysicalStepPreview((existing,))
    with pytest.raises(RuntimeError, match="release has no durable lease"):
        preview.delete_resource_reservation("foreign")
    assert preview.resource_reservations() == (existing,)
    assert preview.grants == []
    assert preview.releases == []


@pytest.mark.parametrize(
    "marker, wait_exists, error",
    [
        (None, False, None),
        ("expected", True, None),
        (None, True, "lacks its lease marker"),
        ("expected", False, "references missing shared wait"),
        ("alien", True, "marker contradicts source trigger"),
        ("alien", False, "marker contradicts source trigger"),
    ],
)
def test_pending_physical_wait_is_fenced_by_independent_durable_lease_marker(
    marker, wait_exists, error,
):
    lease = _lease(
        state="claimed",
        attributes={"physical_wait_id": marker},
    )
    def verify():
        recurring._require_shared_wait_marker(
            lease, wait_id="expected", wait_exists=wait_exists,
        )
    if error:
        with pytest.raises(ResourceConflictError, match=error):
            verify()
    else:
        verify()


def _wait_record(*, attributes: dict | None = None) -> tuple[Entity, dict]:
    expected = {
        "trigger_id": "tick:1",
        "binding_fingerprint": "binding:canon",
        "wait_schema_version": 2,
        "source_digest": "a" * 64,
        "requests": [{"reservation_id": "physical:1"}],
    }
    persisted = {
        **expected,
        "origin_epoch": 1,
        "last_claim_epoch": 2,
    }
    if attributes:
        persisted.update(attributes)
    wait = Entity(
        id="wait:canonical", entity_type="od5a_shared_capacity_wait",
        state="waiting", attributes=persisted, created_at=AT, updated_at=AT,
    )
    return wait, expected


def test_physical_wait_schema_requires_exact_v2_source_provenance():
    wait, expected = _wait_record()
    recurring._validate_shared_wait_record(wait, expected=expected, epoch=2)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_source",
        "bad_state",
        "origin_zero",
        "origin_bool",
        "observed_bool",
        "backward_epoch",
        "future_epoch",
        "version_bool",
        "version_missing",
        "digest_malformed",
        "digest_mismatch",
        "extra_projected_field",
    ],
)
def test_corrupt_physical_wait_cannot_be_certified_by_mutable_projection(mutation):
    wait, expected = _wait_record()
    if mutation == "bad_state":
        wait.state = "released"
    elif mutation == "missing_source":
        expected = None
    elif mutation == "origin_zero":
        wait.attributes["origin_epoch"] = 0
    elif mutation == "origin_bool":
        wait.attributes["origin_epoch"] = True
    elif mutation == "observed_bool":
        wait.attributes["last_claim_epoch"] = True
    elif mutation == "backward_epoch":
        wait.attributes["last_claim_epoch"] = 0
    elif mutation == "future_epoch":
        wait.attributes["last_claim_epoch"] = 3
    elif mutation == "version_bool":
        wait.attributes["wait_schema_version"] = True
    elif mutation == "version_missing":
        del wait.attributes["wait_schema_version"]
    elif mutation == "digest_malformed":
        wait.attributes["source_digest"] = "short"
    elif mutation == "digest_mismatch":
        wait.attributes["source_digest"] = "b" * 64
    elif mutation == "extra_projected_field":
        wait.attributes["unattested_allowance"] = True
    with pytest.raises(ResourceConflictError, match="wait record contradicts"):
        recurring._validate_shared_wait_record(wait, expected=expected, epoch=2)


def test_completed_claim_requires_actual_released_lease_and_expiry_after_update():
    job = _job(completed=True)
    lease = _lease(
        state="released",
        attributes={
            "owner_id": None,
            "trigger_id": job.last_completed_trigger_id,
            "epoch": 1,
            "physical_wait_id": None,
            "expires_at": (AT + timedelta(minutes=2)).isoformat(),
        },
    )
    class _Uow:
        def get_entity(self, kind, identity):
            assert kind == "od5a_runner_lease" and identity == JOB
            return lease

    recurring._assert_completed_released_lease(_Uow(), job)
    lease.attributes["expires_at"] = AT.isoformat()
    with pytest.raises(ValueError, match="completed trigger lacks released lease history"):
        recurring._assert_completed_released_lease(_Uow(), job)


def test_claimed_lease_rejects_unparseable_expiry_before_downstream_effect():
    job = _job(completed=False)
    lease = _lease(
        state="claimed",
        attributes={
            "owner_id": OWNER,
            "trigger_id": job.active_trigger_id,
            "epoch": 1,
            "physical_wait_id": None,
            "expires_at": "not-an-ISO-timestamp",
        },
    )
    class _Uow:
        def get_entity(self, kind, identity):
            assert kind == "od5a_runner_lease" and identity == JOB
            return lease

    with pytest.raises(ValueError, match="active lease contradicts runner claim expiry"):
        recurring._assert_active_claimed_lease(_Uow(), job)
    lease.attributes["expires_at"] = (AT + timedelta(minutes=1)).isoformat()
    recurring._assert_active_claimed_lease(_Uow(), job)


def test_unknown_recurring_job_has_no_implicit_provisional_source():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        with pytest.raises(KeyError, match="not initialized"):
            recurring._read_job(uow, JOB)
    assert tuple(persistence.entities()) == ()


def test_reference_clock_refuses_naive_claim_and_negative_fencing_lease():
    with pytest.raises(ValueError, match="timezone-aware"):
        recurring._require_wall_clock(datetime(2026, 10, 25, 12))
    with pytest.raises(ValueError, match="runner lease must be positive"):
        recurring._require_wall_clock(AT, timedelta(0))
    with pytest.raises(ValueError, match="runner lease must be positive"):
        recurring._require_wall_clock(AT, timedelta(seconds=-1))
    recurring._require_wall_clock(AT, timedelta(seconds=1))


def test_reference_trusted_clock_normalizes_timezone_and_rejects_naive_injection():
    store = MemoryPersistence()
    observed = AT.astimezone(timezone(timedelta(hours=-3)))
    assert recurring._trusted_now(store, lambda: observed) == AT
    with pytest.raises(ValueError, match="timezone-aware"):
        recurring._trusted_now(store, lambda: AT.replace(tzinfo=None))


def test_persisted_shared_binding_requires_same_immutable_pool_fingerprint():
    pool = ResourcePoolContract(
        resource_type="technician", pool_id="od5a-execution",
        scope="shared", capacity=2,
        instance_ids=("physical:0", "physical:1"),
    )
    binding = recurring.SharedPhysicalPoolBinding(
        pool=pool, execution_instances=(
            ("execution:0", "physical:0"),
            ("execution:1", "physical:1"),
        ),
    )
    assert binding.physical_instance("execution:1") == "physical:1"
    assert binding.physical_instance("other") is None
    source = Entity(
        id=JOB, entity_type="od5a", state="active",
        attributes={"shared_pool_binding": binding.fingerprint()},
    )
    recurring._assert_shared_binding(source, binding)
    with pytest.raises(ValueError, match="explicit immutable binding"):
        recurring._assert_shared_binding(source, None)
    source.attributes["shared_pool_binding"] = "forged"
    with pytest.raises(ValueError, match="binding differs"):
        recurring._assert_shared_binding(source, binding)


@pytest.mark.parametrize(
    "bad_slots, error",
    [
        ((("execution:0", "physical:0"), ("execution:0", "physical:1")), "may not alias"),
        ((("execution:0", "physical:0"), ("execution:1", "physical:0")), "may not alias"),
        ((("inventory:0", "physical:0"),), "only execution slots"),
        ((("execution:0", "foreign"),), "not a member"),
        ((("execution:0", "physical:0", "extra"),), "immutable"),
    ],
)
def test_shared_execution_slot_mapping_cannot_alias_or_forge_capacity(bad_slots, error):
    pool = ResourcePoolContract(
        resource_type="technician", pool_id="od5a-execution",
        scope="shared", capacity=2,
        instance_ids=("physical:0", "physical:1"),
    )
    with pytest.raises(ValueError, match=error):
        recurring.SharedPhysicalPoolBinding(pool=pool, execution_instances=bad_slots)

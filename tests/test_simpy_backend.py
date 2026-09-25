from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("simpy")

from sose.backends import ContainerRequest, ContainerSnapshot, ResourceLease, ResourceSnapshot, ScheduledCall
from sose.backends.simpy import SimPyBackend

ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def backend() -> SimPyBackend:
    return SimPyBackend(origin=ORIGIN)


def test_backend_starts_at_origin_without_exposing_simpy_objects():
    runtime = backend()

    assert runtime.name == "simpy"
    assert runtime.now == ORIGIN
    assert not hasattr(runtime, "env")


def test_schedule_after_executes_at_logical_time():
    runtime = backend()
    observed = []

    call = runtime.schedule_after(
        timedelta(hours=2),
        lambda: observed.append(runtime.now),
        key=("demo", 1),
    )

    assert isinstance(call, ScheduledCall)
    assert observed == []

    runtime.run_until(ORIGIN + timedelta(hours=2))

    assert observed == [ORIGIN + timedelta(hours=2)]


def test_same_time_callbacks_follow_priority_then_schedule_order():
    runtime = backend()
    observed = []
    at = ORIGIN + timedelta(hours=1)

    runtime.schedule_at(at, lambda: observed.append("normal-first"), priority=100)
    runtime.schedule_at(at, lambda: observed.append("urgent"), priority=10)
    runtime.schedule_at(at, lambda: observed.append("normal-second"), priority=100)

    runtime.run_until(at)

    assert observed == ["urgent", "normal-first", "normal-second"]


def test_cancelled_callback_never_executes():
    runtime = backend()
    observed = []

    call = runtime.schedule_after(timedelta(minutes=30), lambda: observed.append("ran"))
    assert runtime.cancel(call) is True
    assert runtime.cancel(call) is True

    runtime.run_until(ORIGIN + timedelta(hours=1))

    assert observed == []
    assert runtime.cancel(call) is False


def test_step_processes_exactly_one_backend_event():
    runtime = backend()
    observed = []

    runtime.schedule_after(timedelta(minutes=10), lambda: observed.append(1))
    runtime.schedule_after(timedelta(minutes=20), lambda: observed.append(2))

    assert runtime.step() is True
    assert observed == [1]
    assert runtime.now == ORIGIN + timedelta(minutes=10)

    assert runtime.step() is True
    assert observed == [1, 2]
    assert runtime.step() is False


def test_run_until_processes_events_at_boundary_before_returning():
    runtime = backend()
    observed = []
    boundary = ORIGIN + timedelta(hours=1)

    runtime.schedule_at(boundary, lambda: observed.append("boundary"))

    runtime.run_until(boundary)

    assert observed == ["boundary"]
    assert runtime.now == boundary


def test_run_until_advances_clock_even_when_no_domain_callback_exists():
    runtime = backend()
    target = ORIGIN + timedelta(hours=3)

    processed = runtime.run_until(target)

    assert processed == 1
    assert runtime.now == target


def test_cannot_schedule_or_run_backwards():
    runtime = backend()
    runtime.run_until(ORIGIN + timedelta(hours=1))

    with pytest.raises(ValueError, match="past"):
        runtime.schedule_at(ORIGIN, lambda: None)

    with pytest.raises(ValueError, match="backwards"):
        runtime.run_until(ORIGIN)


def test_resource_grants_up_to_capacity_and_queues_rest():
    runtime = backend()
    runtime.create_resource("technicians", capacity=1)
    acquired: list[ResourceLease] = []

    runtime.request_resource(
        "technicians",
        request_id="wo-1",
        priority=100,
        on_acquired=acquired.append,
    )
    runtime.request_resource(
        "technicians",
        request_id="wo-2",
        priority=100,
        on_acquired=acquired.append,
    )

    snapshot = runtime.resource_snapshot("technicians")
    assert isinstance(snapshot, ResourceSnapshot)
    assert snapshot.capacity == 1
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    runtime.step()

    assert [lease.request_id for lease in acquired] == ["wo-1"]


def test_releasing_resource_grants_next_queued_request():
    runtime = backend()
    runtime.create_resource("technicians", capacity=1)
    acquired: list[ResourceLease] = []

    runtime.request_resource("technicians", request_id="wo-1", on_acquired=acquired.append)
    runtime.request_resource("technicians", request_id="wo-2", on_acquired=acquired.append)

    runtime.step()
    first = acquired[0]
    runtime.release_resource(first)

    # Process the release/grant callbacks until the next acquisition is observed.
    while len(acquired) < 2:
        assert runtime.step() is True

    assert [lease.request_id for lease in acquired] == ["wo-1", "wo-2"]
    assert acquired[1].acquired_at == ORIGIN


def test_priority_resource_grants_higher_priority_waiter_first():
    runtime = backend()
    runtime.create_resource("technicians", capacity=1)
    acquired: list[ResourceLease] = []

    runtime.request_resource("technicians", request_id="holder", on_acquired=acquired.append)
    runtime.request_resource(
        "technicians", request_id="normal", priority=100, on_acquired=acquired.append
    )
    runtime.request_resource(
        "technicians", request_id="emergency", priority=1, on_acquired=acquired.append
    )

    runtime.step()
    runtime.release_resource(acquired[0])

    while len(acquired) < 2:
        assert runtime.step() is True

    assert acquired[1].request_id == "emergency"


def test_resource_request_and_lease_ids_are_backend_neutral():
    runtime = backend()
    runtime.create_resource("bay", capacity=1)
    acquired: list[ResourceLease] = []

    request = runtime.request_resource(
        "bay",
        request_id="inspection-1",
        on_acquired=acquired.append,
    )
    runtime.step()

    assert request.request_id == "inspection-1"
    assert request.resource_name == "bay"
    assert isinstance(acquired[0], ResourceLease)
    assert type(acquired[0]).__module__ == "sose.backends.base"


def test_duplicate_resource_or_request_is_rejected():
    runtime = backend()
    runtime.create_resource("bay")

    with pytest.raises(ValueError, match="already exists"):
        runtime.create_resource("bay")

    runtime.request_resource("bay", request_id="r1", on_acquired=lambda _: None)
    with pytest.raises(ValueError, match="already exists"):
        runtime.request_resource("bay", request_id="r1", on_acquired=lambda _: None)


def test_unknown_resource_and_lease_fail_explicitly():
    runtime = backend()

    with pytest.raises(KeyError, match="unknown resource"):
        runtime.resource_snapshot("missing")

    with pytest.raises(KeyError, match="unknown resource lease"):
        runtime.release_resource("missing")



def test_backend_accepts_equivalent_aware_datetime_in_another_timezone():
    runtime = backend()
    same_instant = (ORIGIN + timedelta(hours=1)).astimezone(timezone(timedelta(hours=1)))
    observed = []

    runtime.schedule_at(same_instant, lambda: observed.append(runtime.now))
    runtime.run_until(ORIGIN + timedelta(hours=1))

    assert observed == [ORIGIN + timedelta(hours=1)]


def test_backend_rejects_mixed_naive_and_aware_datetimes():
    runtime = backend()

    with pytest.raises(ValueError, match="timezone awareness"):
        runtime.schedule_at(datetime(2026, 1, 1, 9), lambda: None)


def test_duplicate_request_id_is_rejected_across_different_resources():
    runtime = backend()
    runtime.create_resource("bay-a")
    runtime.create_resource("bay-b")

    runtime.request_resource("bay-a", request_id="shared", on_acquired=lambda _: None)

    with pytest.raises(ValueError, match="already exists"):
        runtime.request_resource("bay-b", request_id="shared", on_acquired=lambda _: None)


def test_aware_datetime_conversion_respects_dst_offset_changes():
    new_york = ZoneInfo("America/New_York")
    origin = datetime(2026, 1, 1, 8, tzinfo=new_york)
    runtime = SimPyBackend(origin=origin)
    target = datetime(2026, 7, 1, 8, tzinfo=new_york)
    observed = []

    runtime.schedule_at(target, lambda: observed.append(runtime.now))
    runtime.run_until(target)

    assert observed == [target]
    assert runtime.now == target


def test_container_exposes_initial_level_and_capacity():
    runtime = backend()
    runtime.create_container("fuel", capacity=100.0, initial=25.0)

    snapshot = runtime.container_snapshot("fuel")

    assert isinstance(snapshot, ContainerSnapshot)
    assert snapshot.capacity == 100.0
    assert snapshot.level == 25.0
    assert snapshot.queued_puts == 0
    assert snapshot.queued_gets == 0


def test_container_put_and_get_update_level():
    runtime = backend()
    runtime.create_container("fuel", capacity=100.0, initial=25.0)
    completed: list[ContainerRequest] = []

    runtime.put_container(
        "fuel",
        request_id="delivery",
        amount=15.0,
        on_completed=completed.append,
    )
    runtime.get_container(
        "fuel",
        request_id="consume",
        amount=10.0,
        on_completed=completed.append,
    )
    runtime.run_until(ORIGIN)

    assert [request.request_id for request in completed] == ["delivery", "consume"]
    assert runtime.container_snapshot("fuel").level == 30.0


def test_container_get_waits_until_enough_level_is_available():
    runtime = backend()
    runtime.create_container("fuel", capacity=100.0, initial=5.0)
    completed: list[ContainerRequest] = []

    runtime.get_container(
        "fuel",
        request_id="consume",
        amount=10.0,
        on_completed=completed.append,
    )
    runtime.run_until(ORIGIN)

    assert completed == []
    assert runtime.container_snapshot("fuel").queued_gets == 1

    runtime.put_container(
        "fuel",
        request_id="delivery",
        amount=10.0,
        on_completed=completed.append,
    )
    runtime.run_until(ORIGIN)

    assert [request.request_id for request in completed] == ["delivery", "consume"]
    assert runtime.container_snapshot("fuel").level == 5.0


def test_container_put_waits_until_capacity_is_available():
    runtime = backend()
    runtime.create_container("buffer", capacity=10.0, initial=9.0)
    completed: list[ContainerRequest] = []

    runtime.put_container(
        "buffer",
        request_id="fill",
        amount=5.0,
        on_completed=completed.append,
    )
    runtime.run_until(ORIGIN)

    assert completed == []
    assert runtime.container_snapshot("buffer").queued_puts == 1

    runtime.get_container(
        "buffer",
        request_id="drain",
        amount=5.0,
        on_completed=completed.append,
    )
    runtime.run_until(ORIGIN)

    assert [request.request_id for request in completed] == ["drain", "fill"]
    assert runtime.container_snapshot("buffer").level == 9.0


def test_container_rejects_invalid_levels_amounts_and_duplicate_requests():
    runtime = backend()

    with pytest.raises(ValueError, match="capacity"):
        runtime.create_container("invalid", capacity=0)

    with pytest.raises(ValueError, match="initial"):
        runtime.create_container("invalid", capacity=10, initial=11)

    runtime.create_container("fuel", capacity=10, initial=5)

    with pytest.raises(ValueError, match="amount"):
        runtime.get_container("fuel", request_id="zero", amount=0, on_completed=lambda _: None)

    runtime.get_container("fuel", request_id="consume", amount=1, on_completed=lambda _: None)
    with pytest.raises(ValueError, match="already exists"):
        runtime.put_container("fuel", request_id="consume", amount=1, on_completed=lambda _: None)

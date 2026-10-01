from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("simpy")

from sose.backends.simpy import SimPyBackend


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def backend():
    return SimPyBackend(origin=ORIGIN)


def test_schedule_after_rejects_negative_delay():
    runtime = backend()

    with pytest.raises(ValueError, match="delay cannot be negative"):
        runtime.schedule_after(timedelta(seconds=-1), lambda: None)


def test_schedule_key_must_be_unique_while_pending():
    runtime = backend()
    at = ORIGIN + timedelta(hours=1)
    runtime.schedule_at(at, lambda: None, key=("same",))

    with pytest.raises(ValueError, match="scheduled call already exists"):
        runtime.schedule_at(at, lambda: None, key=("same",))


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        ("create_store", "store name cannot be empty"),
        ("create_priority_store", "store name cannot be empty"),
        ("create_filter_store", "store name cannot be empty"),
    ],
)
def test_store_name_validation(factory, message):
    runtime = backend()

    with pytest.raises(ValueError, match=message):
        getattr(runtime, factory)("")


def test_store_rejects_invalid_capacity_and_duplicate_name():
    runtime = backend()

    with pytest.raises(ValueError, match="capacity must be >= 1"):
        runtime.create_store("bad", capacity=0)

    runtime.create_store("queue")
    with pytest.raises(ValueError, match="store already exists"):
        runtime.create_priority_store("queue")


def test_store_rejects_empty_item_id_and_duplicate_item_across_stores():
    runtime = backend()
    runtime.create_store("a")
    runtime.create_store("b")

    with pytest.raises(ValueError, match="item_id cannot be empty"):
        runtime.put_store("a", item_id="", value=1)

    runtime.put_store("a", item_id="shared", value=1)
    with pytest.raises(ValueError, match="store item already exists"):
        runtime.put_store("b", item_id="shared", value=2)


def test_store_get_rejects_empty_or_duplicate_request_id():
    runtime = backend()
    runtime.create_store("queue")

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.get_store("queue", request_id="", on_received=lambda _: None)

    runtime.get_store("queue", request_id="r1", on_received=lambda _: None)
    with pytest.raises(ValueError, match="store request already exists"):
        runtime.get_store("queue", request_id="r1", on_received=lambda _: None)


def test_filter_is_rejected_for_non_filter_store():
    runtime = backend()
    runtime.create_store("queue")

    with pytest.raises(ValueError, match="filters are supported only"):
        runtime.get_store(
            "queue",
            request_id="filtered",
            on_received=lambda _: None,
            filter=lambda _: True,
        )


def test_unknown_store_fails_explicitly():
    runtime = backend()

    with pytest.raises(KeyError, match="unknown store"):
        runtime.store_snapshot("missing")


def test_container_rejects_empty_name_nonfinite_values_and_duplicate_name():
    runtime = backend()

    with pytest.raises(ValueError, match="container name cannot be empty"):
        runtime.create_container("", capacity=1)

    with pytest.raises(ValueError, match="capacity must be finite"):
        runtime.create_container("inf", capacity=float("inf"))

    with pytest.raises(ValueError, match="initial level"):
        runtime.create_container("nan", capacity=10, initial=float("nan"))

    runtime.create_container("tank", capacity=10)
    with pytest.raises(ValueError, match="container already exists"):
        runtime.create_container("tank", capacity=10)


def test_container_request_rejects_empty_id_nonfinite_amount_and_unknown_container():
    runtime = backend()
    runtime.create_container("tank", capacity=10, initial=5)

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.get_container(
            "tank",
            request_id="",
            amount=1,
            on_completed=lambda _: None,
        )

    with pytest.raises(ValueError, match="amount must be finite"):
        runtime.put_container(
            "tank",
            request_id="inf",
            amount=float("inf"),
            on_completed=lambda _: None,
        )

    with pytest.raises(KeyError, match="unknown container"):
        runtime.container_snapshot("missing")


def test_resource_rejects_empty_name_invalid_capacity_and_cross_kind_duplicate():
    runtime = backend()

    with pytest.raises(ValueError, match="resource name cannot be empty"):
        runtime.create_resource("")

    with pytest.raises(ValueError, match="capacity must be >= 1"):
        runtime.create_resource("bad", capacity=0)

    runtime.create_resource("shared")
    with pytest.raises(ValueError, match="resource already exists"):
        runtime.create_preemptive_resource("shared")


def test_resource_request_rejects_empty_id():
    runtime = backend()
    runtime.create_resource("workers")

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.request_resource("workers", request_id="", on_acquired=lambda _: None)


def test_cancel_resource_request_covers_leased_pending_and_unknown_states():
    runtime = backend()
    runtime.create_resource("workers", capacity=1)
    acquired = []

    runtime.request_resource("workers", request_id="holder", on_acquired=acquired.append)
    runtime.request_resource("workers", request_id="queued", on_acquired=acquired.append)

    assert runtime.cancel_resource_request("queued") is True
    runtime.step()
    assert acquired and acquired[0].request_id == "holder"
    assert runtime.cancel_resource_request("holder") is False
    assert runtime.cancel_resource_request("missing") is False


def test_preemptive_resource_validates_name_capacity_and_request_identity():
    runtime = backend()

    with pytest.raises(ValueError, match="resource name cannot be empty"):
        runtime.create_preemptive_resource("")

    with pytest.raises(ValueError, match="capacity must be >= 1"):
        runtime.create_preemptive_resource("bad", capacity=0)

    runtime.create_preemptive_resource("bay")
    with pytest.raises(ValueError, match="resource already exists"):
        runtime.create_preemptive_resource("bay")

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.request_preemptive_resource(
            "bay",
            request_id="",
            on_acquired=lambda _: None,
            on_preempted=lambda _: None,
        )


def test_request_id_is_unique_across_normal_and_preemptive_resources():
    runtime = backend()
    runtime.create_resource("workers")
    runtime.create_preemptive_resource("bay")
    runtime.request_resource("workers", request_id="shared", on_acquired=lambda _: None)

    with pytest.raises(ValueError, match="resource request already exists"):
        runtime.request_preemptive_resource(
            "bay",
            request_id="shared",
            on_acquired=lambda _: None,
            on_preempted=lambda _: None,
        )


def test_cancel_preemptive_acquired_request_releases_lease():
    runtime = backend()
    runtime.create_preemptive_resource("bay")
    acquired = []
    runtime.request_preemptive_resource(
        "bay",
        request_id="active",
        on_acquired=acquired.append,
        on_preempted=lambda _: None,
        preempt=False,
    )
    runtime.run_until(ORIGIN)
    assert acquired and runtime.preemptive_resource_snapshot("bay").in_use == 1

    assert runtime.cancel_preemptive_resource_request("active") is True
    runtime.run_until(ORIGIN)
    assert runtime.preemptive_resource_snapshot("bay").in_use == 0


def test_unknown_preemptive_resource_and_lease_fail_explicitly():
    runtime = backend()

    with pytest.raises(KeyError, match="unknown preemptive resource"):
        runtime.preemptive_resource_snapshot("missing")

    with pytest.raises(KeyError, match="unknown preemptive resource lease"):
        runtime.release_preemptive_resource("missing")


def test_naive_backend_rejects_aware_datetime_and_preserves_naive_now():
    origin = datetime(2026, 1, 1, 8)
    runtime = SimPyBackend(origin=origin)

    assert runtime.now == origin
    assert runtime.now.tzinfo is None

    with pytest.raises(ValueError, match="timezone awareness"):
        runtime.schedule_at(
            datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
            lambda: None,
        )

    runtime.run_until(origin + timedelta(hours=1))
    assert runtime.now == origin + timedelta(hours=1)

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

import pytest

pytest.importorskip("simpy")

from sose.backends.simpy import SimPyBackend


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _backend():
    return SimPyBackend(origin=ORIGIN)


def test_schedule_after_rejects_negative_delay_and_duplicate_identity():
    runtime = _backend()

    with pytest.raises(ValueError, match="delay cannot be negative"):
        runtime.schedule_after(timedelta(seconds=-1), lambda: None)

    at = ORIGIN + timedelta(minutes=1)
    runtime.schedule_at(at, lambda: None, key=("same",))
    with pytest.raises(ValueError, match="scheduled call already exists"):
        runtime.schedule_at(at, lambda: None, key=("same",))


@pytest.mark.parametrize(
    ("name", "capacity", "message"),
    [
        ("", None, "name cannot be empty"),
        ("bad", 0, "capacity must be >= 1"),
    ],
)
def test_store_definition_validation(name, capacity, message):
    runtime = _backend()

    with pytest.raises(ValueError, match=message):
        runtime.create_store(name, capacity=capacity)


def test_store_duplicate_unknown_and_unbounded_snapshot_contracts():
    runtime = _backend()
    runtime.create_store("fifo")

    assert runtime.store_snapshot("fifo").capacity is None

    with pytest.raises(ValueError, match="already exists"):
        runtime.create_priority_store("fifo")

    with pytest.raises(KeyError, match="unknown store"):
        runtime.store_snapshot("missing")


def test_store_item_and_request_identity_validation():
    runtime = _backend()
    runtime.create_store("a")
    runtime.create_store("b")

    with pytest.raises(ValueError, match="item_id cannot be empty"):
        runtime.put_store("a", item_id="", value=1)

    runtime.put_store("a", item_id="shared", value=1)
    with pytest.raises(ValueError, match="store item already exists"):
        runtime.put_store("b", item_id="shared", value=2)

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.get_store("a", request_id="", on_received=lambda _: None)

    runtime.get_store("a", request_id="request-1", on_received=lambda _: None)
    with pytest.raises(ValueError, match="store request already exists"):
        runtime.get_store("b", request_id="request-1", on_received=lambda _: None)


def test_filter_is_rejected_for_non_filter_store():
    runtime = _backend()
    runtime.create_store("fifo")

    with pytest.raises(ValueError, match="filters are supported only by filter stores"):
        runtime.get_store(
            "fifo",
            request_id="filtered",
            on_received=lambda _: None,
            filter=lambda _: True,
        )

    received = []
    runtime.put_store("fifo", item_id="item", value=1)
    runtime.get_store(
        "fifo",
        request_id="filtered",
        on_received=received.append,
    )
    runtime.run_until(ORIGIN)
    assert [item.item_id for item in received] == ["item"]


def test_priority_store_orders_by_priority_then_insertion():
    runtime = _backend()
    runtime.create_priority_store("priority")
    received = []

    runtime.put_store("priority", item_id="normal", value="normal", priority=100)
    runtime.put_store("priority", item_id="urgent-1", value="urgent-1", priority=1)
    runtime.put_store("priority", item_id="urgent-2", value="urgent-2", priority=1)

    for request_id in ("g1", "g2", "g3"):
        runtime.get_store(
            "priority",
            request_id=request_id,
            on_received=lambda item: received.append(item.item_id),
        )

    runtime.run_until(ORIGIN)
    assert received == ["urgent-1", "urgent-2", "normal"]


def test_filter_store_selects_matching_item_and_keeps_other_item():
    runtime = _backend()
    runtime.create_filter_store("filter")
    received = []

    runtime.put_store("filter", item_id="a", value={"kind": "a"})
    runtime.put_store("filter", item_id="b", value={"kind": "b"})
    runtime.get_store(
        "filter",
        request_id="get-b",
        on_received=lambda item: received.append(item.item_id),
        filter=lambda item: item.value["kind"] == "b",
    )

    runtime.run_until(ORIGIN)

    assert received == ["b"]
    assert runtime.store_snapshot("filter").size == 1


@pytest.mark.parametrize(
    ("name", "capacity", "initial", "message"),
    [
        ("", 1.0, 0.0, "name cannot be empty"),
        ("bad", math.inf, 0.0, "capacity must be finite"),
        ("bad", math.nan, 0.0, "capacity must be finite"),
        ("bad", 1.0, math.nan, "initial level"),
        ("bad", 1.0, -1.0, "initial level"),
    ],
)
def test_container_definition_validation(name, capacity, initial, message):
    runtime = _backend()

    with pytest.raises(ValueError, match=message):
        runtime.create_container(name, capacity=capacity, initial=initial)


def test_container_duplicate_unknown_request_identity_and_amount_validation():
    runtime = _backend()
    runtime.create_container("a", capacity=10, initial=5)
    runtime.create_container("b", capacity=10, initial=5)

    with pytest.raises(ValueError, match="already exists"):
        runtime.create_container("a", capacity=10)

    with pytest.raises(KeyError, match="unknown container"):
        runtime.container_snapshot("missing")

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.get_container(
            "a", request_id="", amount=1, on_completed=lambda _: None
        )

    with pytest.raises(ValueError, match="amount"):
        runtime.put_container(
            "a", request_id="nan", amount=math.nan, on_completed=lambda _: None
        )

    runtime.get_container(
        "a", request_id="shared", amount=1, on_completed=lambda _: None
    )
    with pytest.raises(ValueError, match="already exists"):
        runtime.put_container(
            "b", request_id="shared", amount=1, on_completed=lambda _: None
        )


@pytest.mark.parametrize(
    ("name", "capacity", "message"),
    [
        ("", 1, "name cannot be empty"),
        ("bad", 0, "capacity must be >= 1"),
    ],
)
def test_resource_definition_validation(name, capacity, message):
    runtime = _backend()

    with pytest.raises(ValueError, match=message):
        runtime.create_resource(name, capacity=capacity)


def test_resource_request_empty_identity_unknown_cancel_and_lease_cancel_contract():
    runtime = _backend()
    runtime.create_resource("worker")

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.request_resource(
            "worker",
            request_id="",
            on_acquired=lambda _: None,
        )

    assert runtime.cancel_resource_request("missing") is False

    acquired = []
    runtime.request_resource(
        "worker",
        request_id="active",
        on_acquired=acquired.append,
    )
    runtime.run_until(ORIGIN)
    assert acquired
    assert runtime.cancel_resource_request("active") is False


def test_pending_resource_request_can_be_cancelled_before_grant():
    runtime = _backend()
    runtime.create_resource("worker", capacity=1)
    acquired = []

    runtime.request_resource("worker", request_id="holder", on_acquired=acquired.append)
    runtime.request_resource("worker", request_id="waiter", on_acquired=acquired.append)
    runtime.run_until(ORIGIN)

    assert [lease.request_id for lease in acquired] == ["holder"]
    assert runtime.resource_snapshot("worker").queued == 1
    assert runtime.cancel_resource_request("waiter") is True
    assert runtime.resource_snapshot("worker").queued == 0


@pytest.mark.parametrize(
    ("name", "capacity", "message"),
    [
        ("", 1, "name cannot be empty"),
        ("bad", 0, "capacity must be >= 1"),
    ],
)
def test_preemptive_resource_definition_validation(name, capacity, message):
    runtime = _backend()

    with pytest.raises(ValueError, match=message):
        runtime.create_preemptive_resource(name, capacity=capacity)


def test_resource_name_namespace_is_symmetric_between_normal_and_preemptive():
    runtime = _backend()
    runtime.create_resource("normal-first")

    with pytest.raises(ValueError, match="already exists"):
        runtime.create_preemptive_resource("normal-first")

    runtime.create_preemptive_resource("preemptive-first")
    with pytest.raises(ValueError, match="already exists"):
        runtime.create_resource("preemptive-first")


def test_preemptive_request_empty_and_duplicate_identity_validation():
    runtime = _backend()
    runtime.create_preemptive_resource("bay")

    with pytest.raises(ValueError, match="request_id cannot be empty"):
        runtime.request_preemptive_resource(
            "bay",
            request_id="",
            on_acquired=lambda _: None,
            on_preempted=lambda _: None,
        )

    runtime.request_preemptive_resource(
        "bay",
        request_id="r1",
        on_acquired=lambda _: None,
        on_preempted=lambda _: None,
        preempt=False,
    )
    with pytest.raises(ValueError, match="already exists"):
        runtime.request_preemptive_resource(
            "bay",
            request_id="r1",
            on_acquired=lambda _: None,
            on_preempted=lambda _: None,
            preempt=False,
        )


def test_request_id_namespace_is_symmetric_between_normal_and_preemptive():
    runtime = _backend()
    runtime.create_resource("normal")
    runtime.create_preemptive_resource("preemptive")

    runtime.request_resource(
        "normal",
        request_id="normal-first",
        on_acquired=lambda _: None,
    )
    with pytest.raises(ValueError, match="already exists"):
        runtime.request_preemptive_resource(
            "preemptive",
            request_id="normal-first",
            on_acquired=lambda _: None,
            on_preempted=lambda _: None,
            preempt=False,
        )

    runtime.request_preemptive_resource(
        "preemptive",
        request_id="preemptive-first",
        on_acquired=lambda _: None,
        on_preempted=lambda _: None,
        preempt=False,
    )
    with pytest.raises(ValueError, match="already exists"):
        runtime.request_resource(
            "normal",
            request_id="preemptive-first",
            on_acquired=lambda _: None,
        )


def test_acquired_preemptive_request_can_be_cancelled_and_unknown_lease_fails():
    runtime = _backend()
    runtime.create_preemptive_resource("bay")
    acquired = []

    runtime.request_preemptive_resource(
        "bay",
        request_id="r1",
        on_acquired=acquired.append,
        on_preempted=lambda _: None,
        preempt=False,
    )
    runtime.run_until(ORIGIN)

    assert acquired
    assert runtime.cancel_preemptive_resource_request("r1") is True
    runtime.run_until(ORIGIN)
    assert runtime.preemptive_resource_snapshot("bay").in_use == 0

    with pytest.raises(KeyError, match="unknown preemptive resource lease"):
        runtime.release_preemptive_resource("missing")


def test_unknown_preemptive_resource_is_explicit():
    runtime = _backend()

    with pytest.raises(KeyError, match="unknown preemptive resource"):
        runtime.preemptive_resource_snapshot("missing")

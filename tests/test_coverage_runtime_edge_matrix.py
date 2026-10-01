from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sose.backends.base import ResourceLease, ResourcePreemption, StoreItem
from sose.core.containers import DurableContainerManager
from sose.core.preemption import DurablePreemptiveResourceManager
from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
    DurableStoreItem,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
    ResourcePreemptionResult,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)
from sose.core.stores import DurableStoreManager
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _item(item_id="item", store_name="inbox", value=1, *, priority=100, sequence=1):
    return DurableStoreItem(item_id, store_name, value, priority, sequence)


def _put(item_id="item", store_name="inbox", value=1, *, priority=100, sequence=1):
    return StorePutIntent(item_id, store_name, value, priority, NOW, sequence)


def _get(request_id="get", store_name="inbox", *, sequence=1, filter_key=None):
    return StoreGetRequest(request_id, store_name, NOW, sequence, filter_key)


def _get_result(request_id="get", store_name="inbox", item=None, *, sequence=1):
    return StoreGetResult(
        request_id,
        store_name,
        item or _item(store_name=store_name),
        NOW,
        sequence,
    )


@pytest.mark.parametrize(
    ("seed", "message"),
    [
        (
            lambda s: s.store_items.__setitem__(
                "orphan", _item("orphan", store_name="missing")
            ),
            "store item references unknown definition",
        ),
        (
            lambda s: s.store_put_intents.__setitem__(
                "orphan", _put("orphan", store_name="missing")
            ),
            "store put references unknown definition",
        ),
        (
            lambda s: s.store_get_requests.__setitem__(
                "orphan", _get("orphan", store_name="missing")
            ),
            "store get references unknown definition",
        ),
    ],
)
def test_store_rebuild_rejects_orphaned_records(seed, message):
    persistence = MemoryPersistence()
    seed(persistence._state)

    with pytest.raises(RuntimeError, match=message):
        DurableStoreManager(persistence).validate_rebuild()


def test_store_rebuild_rejects_filtered_get_on_non_filter_store():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    persistence._state.store_get_requests["get"] = _get(
        filter_key="wanted",
    )

    with pytest.raises(RuntimeError, match="filtered get targets non-filter store"):
        DurableStoreManager(
            persistence, filters={"wanted": lambda item: True}
        ).validate_rebuild()


def test_store_rebuild_rejects_content_over_capacity():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition(
        "inbox", capacity=1
    )
    persistence._state.store_items["one"] = _item("one", sequence=1)
    persistence._state.store_items["two"] = _item("two", sequence=2)

    with pytest.raises(RuntimeError, match="more items than capacity"):
        DurableStoreManager(persistence).validate_rebuild()


@pytest.mark.parametrize(
    "seed",
    [
        lambda p: p._state.store_items.__setitem__(
            "same", _item("same", value="old")
        ),
        lambda p: p._state.store_get_results.__setitem__(
            "done",
            _get_result("done", item=_item("same", value="old")),
        ),
        lambda p: p._state.store_put_intents.__setitem__(
            "same", _put("same", value="old")
        ),
    ],
)
def test_ensure_put_rejects_identity_conflicts_across_all_durable_phases(seed):
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    seed(persistence)

    with pytest.raises(ValueError, match="store item identity conflict"):
        DurableStoreManager(persistence).ensure_put(
            object(),
            store_name="inbox",
            item_id="same",
            value="new",
            requested_at=NOW,
        )


def test_get_rejects_filter_key_for_fifo_store_and_unknown_definition():
    persistence = MemoryPersistence()
    manager = DurableStoreManager(persistence)
    manager.define(StoreDefinition("inbox"))

    with pytest.raises(ValueError, match="filter_key"):
        manager.get(
            object(),
            store_name="inbox",
            request_id="bad-filter",
            requested_at=NOW,
            filter_key="wanted",
        )
    with pytest.raises(KeyError, match="unknown store definition"):
        manager.get(
            object(),
            store_name="missing",
            request_id="missing",
            requested_at=NOW,
        )


def test_commit_put_handles_existing_missing_and_changed_intent(monkeypatch):
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    persistence._state.store_items["existing"] = _item("existing")

    manager = DurableStoreManager(persistence)
    assert manager.commit_put("existing") == _item("existing")
    assert manager.commit_put("missing") is None

    persistence._state.store_put_intents["pending"] = _put("pending")

    @contextmanager
    def changed_transaction():
        yield SimpleNamespace(
            get_store_put_intent=lambda item_id: _put(item_id, value="changed")
        )

    monkeypatch.setattr(persistence, "transaction", changed_transaction)
    with pytest.raises(RuntimeError, match="intent changed before commit"):
        manager.commit_put("pending")


def test_commit_get_covers_terminal_missing_and_wrong_store_paths():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    manager = DurableStoreManager(persistence)

    terminal = _get_result("done")
    persistence._state.store_get_results["done"] = terminal
    assert manager.commit_get(
        "done",
        StoreItem("ignored", "inbox", 1),
        completed_at=NOW,
    ) == terminal

    with pytest.raises(KeyError, match="unknown store get request"):
        manager.commit_get(
            "missing",
            StoreItem("item", "inbox", 1),
            completed_at=NOW,
        )

    persistence._state.store_get_requests["wrong"] = _get("wrong")
    with pytest.raises(RuntimeError, match="wrong store"):
        manager.commit_get(
            "wrong",
            StoreItem("item", "other", 1),
            completed_at=NOW,
        )

    persistence._state.store_get_requests["unknown-item"] = _get("unknown-item")
    with pytest.raises(KeyError, match="unknown durable store item"):
        manager.commit_get(
            "unknown-item",
            StoreItem("ghost", "inbox", 1),
            completed_at=NOW,
        )


def test_commit_get_detects_request_and_item_races(monkeypatch):
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    request = _get("race")
    item = _item("item")
    persistence._state.store_get_requests["race"] = request
    persistence._state.store_items["item"] = item
    manager = DurableStoreManager(persistence)

    class ChangedRequestUow:
        def get_store_get_request(self, request_id):
            return None

        def get_store_get_result(self, request_id):
            return None

    @contextmanager
    def changed_request_transaction():
        yield ChangedRequestUow()

    monkeypatch.setattr(persistence, "transaction", changed_request_transaction)
    with pytest.raises(RuntimeError, match="store get request changed"):
        manager.commit_get(
            "race",
            StoreItem("item", "inbox", 1),
            completed_at=NOW,
        )

    class DisappearedItemUow:
        def get_store_get_request(self, request_id):
            return request

        def get_store_item(self, item_id):
            return None

        def get_store_put_intent(self, item_id):
            return None

    @contextmanager
    def disappeared_item_transaction():
        yield DisappearedItemUow()

    monkeypatch.setattr(persistence, "transaction", disappeared_item_transaction)
    with pytest.raises(RuntimeError, match="store item disappeared"):
        manager.commit_get(
            "race",
            StoreItem("item", "inbox", 1),
            completed_at=NOW,
        )


def test_ensure_selection_rejects_mismatched_pending_request():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["one"] = StoreDefinition("one")
    persistence._state.store_definitions["two"] = StoreDefinition("two")
    persistence._state.store_get_requests["pick"] = _get("pick", "one")

    with pytest.raises(RuntimeError, match="does not match requested store/filter"):
        DurableStoreManager(persistence).ensure_selection(
            object(),
            store_name="two",
            request_id="pick",
            requested_at=NOW,
        )



def test_ensure_put_returns_matching_consumed_item():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    consumed = _item("same", value="stable")
    persistence._state.store_get_results["done"] = _get_result(
        "done", item=consumed
    )

    result = DurableStoreManager(persistence).ensure_put(
        object(),
        store_name="inbox",
        item_id="same",
        value="stable",
        requested_at=NOW,
    )

    assert result == consumed


def test_commit_get_returns_concurrent_terminal_result(monkeypatch):
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    request = _get("race")
    item = _item("item")
    terminal = _get_result("race", item=item)
    persistence._state.store_get_requests["race"] = request
    persistence._state.store_items["item"] = item
    manager = DurableStoreManager(persistence)

    class ConcurrentWinnerUow:
        def get_store_get_request(self, request_id):
            return None

        def get_store_get_result(self, request_id):
            return terminal

    @contextmanager
    def transaction():
        yield ConcurrentWinnerUow()

    monkeypatch.setattr(persistence, "transaction", transaction)

    assert manager.commit_get(
        "race",
        StoreItem("item", "inbox", 1),
        completed_at=NOW,
    ) == terminal


def test_commit_get_consumes_item_still_owned_by_pending_put_intent():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    persistence._state.store_get_requests["get"] = _get("get", sequence=2)
    persistence._state.store_put_intents["item"] = _put(
        "item", value="payload", sequence=1
    )

    result = DurableStoreManager(persistence).commit_get(
        "get",
        StoreItem("item", "inbox", "payload"),
        completed_at=NOW,
    )

    assert result.item.item_id == "item"
    assert persistence.store_put_intents() == ()
    assert persistence.store_get_requests() == ()
    assert persistence.store_get_results() == (result,)


def test_store_put_invokes_durable_completion_callback():
    persistence = MemoryPersistence()
    manager = DurableStoreManager(persistence)
    manager.define(StoreDefinition("inbox"))
    observed = []

    class ImmediateStoreBackend:
        def put_store(
            self,
            name,
            *,
            item_id,
            value,
            priority=100,
            on_stored=None,
        ):
            item = StoreItem(item_id, name, value, priority)
            if on_stored is not None:
                on_stored(item)
            return item

    manager.put(
        ImmediateStoreBackend(),
        store_name="inbox",
        item_id="item",
        value=1,
        requested_at=NOW,
        on_stored=observed.append,
    )

    assert [item.item_id for item in observed] == ["item"]
    assert [item.item_id for item in persistence.store_items()] == ["item"]



def test_store_reconciliation_supports_backends_without_run_until():
    persistence = MemoryPersistence()
    persistence._state.store_definitions["inbox"] = StoreDefinition("inbox")
    persistence._state.store_put_intents["pending"] = _put(
        "pending", value="payload"
    )
    manager = DurableStoreManager(persistence)

    # Matching pending work is already durable; a minimal backend without
    # run_until must not be required merely to reconcile identity.
    assert manager.ensure_put(
        object(),
        store_name="inbox",
        item_id="pending",
        value="payload",
        requested_at=NOW,
    ) is None

    persistence._state.store_get_requests["pick"] = _get("pick")
    assert manager.ensure_selection(
        object(),
        store_name="inbox",
        request_id="pick",
        requested_at=NOW,
    ) is None


def _container_intent(
    request_id="op",
    container_name="fuel",
    operation="put",
    amount=1.0,
    *,
    sequence=1,
):
    return ContainerOperationIntent(
        request_id, container_name, operation, amount, NOW, sequence
    )


def _container_result(
    request_id="op",
    container_name="fuel",
    operation="put",
    amount=1.0,
    *,
    level_before=1.0,
    level_after=2.0,
    sequence=1,
):
    return ContainerOperationResult(
        request_id,
        container_name,
        operation,
        amount,
        NOW,
        level_before,
        level_after,
        sequence,
    )


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda s: s.container_definitions.__setitem__(
                "fuel", ContainerDefinition("fuel", 10.0, 1.0)
            ),
            "missing durable state",
        ),
        (
            lambda s: (
                s.container_definitions.__setitem__(
                    "fuel", ContainerDefinition("fuel", 1.0, 0.0)
                ),
                s.container_states.__setitem__("fuel", ContainerState("fuel", 2.0)),
            ),
            "level exceeds capacity",
        ),
        (
            lambda s: s.container_states.__setitem__(
                "orphan", ContainerState("orphan", 1.0)
            ),
            "state references unknown definition",
        ),
        (
            lambda s: s.container_operation_intents.__setitem__(
                "op", _container_intent(container_name="missing")
            ),
            "operation references unknown definition",
        ),
        (
            lambda s: s.container_operation_results.__setitem__(
                "op", _container_result(container_name="missing")
            ),
            "result references unknown definition",
        ),
    ],
)
def test_container_rebuild_rejects_corrupt_relations(setup, message):
    persistence = MemoryPersistence()
    setup(persistence._state)

    with pytest.raises(RuntimeError, match=message):
        DurableContainerManager(persistence).validate_rebuild()


def test_container_rebuild_rejects_pending_completed_overlap_and_bad_result_math():
    persistence = MemoryPersistence()
    persistence._state.container_definitions["fuel"] = ContainerDefinition(
        "fuel", 10.0, 1.0
    )
    persistence._state.container_states["fuel"] = ContainerState("fuel", 1.0)
    persistence._state.container_operation_intents["same"] = _container_intent("same")
    persistence._state.container_operation_results["same"] = _container_result("same")

    with pytest.raises(RuntimeError, match="both pending and completed"):
        DurableContainerManager(persistence).validate_rebuild()

    persistence._state.container_operation_intents.clear()
    persistence._state.container_operation_results["bad-delta"] = _container_result(
        "bad-delta", level_before=1.0, level_after=5.0
    )
    with pytest.raises(RuntimeError, match="inconsistent level delta"):
        DurableContainerManager(persistence).validate_rebuild()

    persistence._state.container_operation_results["bad-delta"] = _container_result(
        "bad-delta", amount=10.0, level_before=1.0, level_after=11.0
    )
    with pytest.raises(RuntimeError, match="outside capacity"):
        DurableContainerManager(persistence).validate_rebuild()


def test_container_define_rejects_conflicting_existing_state():
    persistence = MemoryPersistence()
    persistence._state.container_states["fuel"] = ContainerState("fuel", 2.0)

    with pytest.raises(ValueError, match="container state already exists"):
        DurableContainerManager(persistence).define(
            ContainerDefinition("fuel", 10.0, 1.0)
        )


def test_container_commit_handles_terminal_missing_state_infeasible_and_unknown_definition():
    persistence = MemoryPersistence()
    persistence._state.container_definitions["fuel"] = ContainerDefinition(
        "fuel", 2.0, 1.0
    )
    persistence._state.container_states["fuel"] = ContainerState("fuel", 1.0)
    manager = DurableContainerManager(persistence)

    terminal = _container_result("done")
    persistence._state.container_operation_results["done"] = terminal
    assert manager.commit("done", completed_at=NOW) == terminal

    with pytest.raises(KeyError, match="unknown container operation intent"):
        manager.commit("missing", completed_at=NOW)

    persistence._state.container_operation_intents["no-state"] = _container_intent(
        "no-state", container_name="ghost"
    )
    with pytest.raises(KeyError, match="unknown durable container state"):
        manager.commit("no-state", completed_at=NOW)

    persistence._state.container_operation_intents["overflow"] = _container_intent(
        "overflow", amount=2.0
    )
    with pytest.raises(RuntimeError, match="infeasible"):
        manager.commit("overflow", completed_at=NOW)

    with pytest.raises(KeyError, match="unknown container definition"):
        manager._definition("missing")



def test_container_commit_handles_concurrent_winner_and_transaction_races(monkeypatch):
    persistence = MemoryPersistence()
    persistence._state.container_definitions["fuel"] = ContainerDefinition(
        "fuel", 10.0, 1.0
    )
    state = ContainerState("fuel", 1.0)
    intent = _container_intent("race")
    persistence._state.container_states["fuel"] = state
    persistence._state.container_operation_intents["race"] = intent
    manager = DurableContainerManager(persistence)
    terminal = _container_result("race")

    class ConcurrentWinnerUow:
        def get_container_operation_intent(self, request_id):
            return None

        def get_container_operation_result(self, request_id):
            return terminal

    @contextmanager
    def concurrent_winner_transaction():
        yield ConcurrentWinnerUow()

    monkeypatch.setattr(persistence, "transaction", concurrent_winner_transaction)
    assert manager.commit("race", completed_at=NOW) == terminal

    class ChangedIntentUow:
        def get_container_operation_intent(self, request_id):
            return None

        def get_container_operation_result(self, request_id):
            return None

    @contextmanager
    def changed_intent_transaction():
        yield ChangedIntentUow()

    monkeypatch.setattr(persistence, "transaction", changed_intent_transaction)
    with pytest.raises(RuntimeError, match="operation intent changed"):
        manager.commit("race", completed_at=NOW)

    class ChangedStateUow:
        def get_container_operation_intent(self, request_id):
            return intent

        def get_container_state(self, name):
            return ContainerState(name, 2.0)

    @contextmanager
    def changed_state_transaction():
        yield ChangedStateUow()

    monkeypatch.setattr(persistence, "transaction", changed_state_transaction)
    with pytest.raises(RuntimeError, match="state changed before operation commit"):
        manager.commit("race", completed_at=NOW)


def _definition(name="crew", capacity=1):
    return PreemptiveResourceDefinition(name, capacity)


def _demand(request_id="request", resource_name="crew", *, sequence=1):
    return PreemptiveResourceDemand(
        request_id, resource_name, 100, True, NOW, sequence
    )


def _reservation(
    reservation_id="reservation",
    request_id="holder",
    resource_name="crew",
    *,
    sequence=1,
):
    return PreemptiveResourceReservation(
        reservation_id, request_id, resource_name, NOW, 100, sequence
    )


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (
            lambda s: s.preemptive_resource_reservations.__setitem__(
                "r", _reservation(resource_name="missing")
            ),
            "reservation references unknown definition",
        ),
        (
            lambda s: s.preemptive_resource_demands.__setitem__(
                "d", _demand(resource_name="missing")
            ),
            "demand references unknown definition",
        ),
        (
            lambda s: s.preemptive_resource_release_intents.__setitem__(
                "release",
                PreemptiveResourceReleaseIntent(
                    "release", "missing-reservation", "crew", NOW
                ),
            ),
            "release references unknown reservation",
        ),
        (
            lambda s: s.resource_preemption_results.__setitem__(
                "result",
                ResourcePreemptionResult(
                    "result",
                    "missing",
                    "old-reservation",
                    "old",
                    "new",
                    "new-reservation",
                    NOW,
                    1,
                ),
            ),
            "preemption result references unknown definition",
        ),
    ],
)
def test_preemptive_rebuild_rejects_orphaned_records(setup, message):
    persistence = MemoryPersistence()
    setup(persistence._state)

    with pytest.raises(RuntimeError, match=message):
        DurablePreemptiveResourceManager(persistence).validate_rebuild()


def test_preemptive_rebuild_rejects_wrong_release_resource_capacity_and_overlap():
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    persistence._state.preemptive_resource_definitions["other"] = _definition("other")
    reservation = _reservation()
    persistence._state.preemptive_resource_reservations["reservation"] = reservation
    persistence._state.preemptive_resource_release_intents["release"] = (
        PreemptiveResourceReleaseIntent(
            "release", "reservation", "other", NOW
        )
    )

    with pytest.raises(RuntimeError, match="release targets wrong resource"):
        DurablePreemptiveResourceManager(persistence).validate_rebuild()

    persistence._state.preemptive_resource_release_intents.clear()
    persistence._state.preemptive_resource_reservations["reservation-2"] = _reservation(
        "reservation-2", "holder-2", sequence=2
    )
    with pytest.raises(RuntimeError, match="exceeds durable capacity"):
        DurablePreemptiveResourceManager(persistence).validate_rebuild()

    persistence._state.preemptive_resource_reservations.pop("reservation-2")
    persistence._state.preemptive_resource_demands["holder"] = _demand("holder", sequence=2)
    with pytest.raises(RuntimeError, match="both pending and reserved"):
        DurablePreemptiveResourceManager(persistence).validate_rebuild()


def test_preemptive_request_duplicate_cancel_release_and_callback_guards():
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    persistence._state.preemptive_resource_demands["duplicate"] = _demand("duplicate")
    manager = DurablePreemptiveResourceManager(persistence)

    with pytest.raises(ValueError, match="request already exists"):
        manager.request(
            object(),
            resource_name="crew",
            request_id="duplicate",
            requested_at=NOW,
        )

    backend = SimpleNamespace(
        cancel_preemptive_resource_request=lambda request_id: False
    )
    assert manager.cancel_pending(backend, "duplicate") is False
    assert manager.release(object(), "missing") is False

    persistence._state.preemptive_resource_reservations["holder"] = _reservation()
    with pytest.raises(RuntimeError, match="backend lease is not reconstructed"):
        manager.release(object(), "reservation")

    manager._record_acquired(
        request_id="missing",
        lease=ResourceLease("lease", "missing", "crew", NOW),
    )
    assert manager._pending_grants == {}

    with pytest.raises(RuntimeError, match="missing preempting request identity"):
        manager._record_preempted(
            ResourcePreemption("lease", "holder", "crew", NOW, preempted_by=None)
        )

    manager._record_preempted(
        ResourcePreemption("lease", "already-gone", "crew", NOW, preempted_by="new")
    )
    assert manager._pending_preemptions == {}




def test_preemptive_reconciliation_supports_backend_without_run_until():
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    persistence._state.preemptive_resource_demands["pending"] = _demand("pending")
    manager = DurablePreemptiveResourceManager(persistence)

    assert manager.ensure_requested(
        object(),
        resource_name="crew",
        request_id="pending",
        requested_at=NOW,
    ) is None

    empty = DurablePreemptiveResourceManager(MemoryPersistence())
    assert empty.withdraw(object(), "missing") is False


def test_preemptive_cancel_detects_demand_race(monkeypatch):
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    demand = _demand("pending")
    persistence._state.preemptive_resource_demands["pending"] = demand
    manager = DurablePreemptiveResourceManager(persistence)

    class ChangedDemandUow:
        def get_preemptive_resource_demand(self, request_id):
            return None

    @contextmanager
    def transaction():
        yield ChangedDemandUow()

    monkeypatch.setattr(persistence, "transaction", transaction)
    backend = SimpleNamespace(
        cancel_preemptive_resource_request=lambda request_id: True
    )
    with pytest.raises(RuntimeError, match="demand changed during cancellation"):
        manager.cancel_pending(backend, "pending")


def test_preemptive_release_returns_false_when_reservation_changes(monkeypatch):
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    reservation = _reservation()
    persistence._state.preemptive_resource_reservations["reservation"] = reservation
    manager = DurablePreemptiveResourceManager(persistence)
    manager._backend_leases["reservation"] = ResourceLease(
        "lease", "holder", "crew", NOW
    )

    class ChangedReservationUow:
        def get_preemptive_resource_reservation(self, reservation_id):
            return None

    @contextmanager
    def transaction():
        yield ChangedReservationUow()

    monkeypatch.setattr(persistence, "transaction", transaction)
    assert manager.release(object(), "reservation") is False


def _seed_preemption_pair(persistence):
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    demand = _demand("urgent", sequence=2)
    displaced = _reservation("old-reservation", "holder", sequence=1)
    persistence._state.preemptive_resource_demands["urgent"] = demand
    persistence._state.preemptive_resource_reservations[
        "old-reservation"
    ] = displaced
    lease = ResourceLease("lease-urgent", "urgent", "crew", NOW)
    event = ResourcePreemption(
        "lease-holder",
        "holder",
        "crew",
        NOW,
        preempted_by="urgent",
    )
    return demand, displaced, lease, event


def test_preemptive_pairing_handles_missing_and_cross_resource_records():
    persistence = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(persistence)
    manager._pending_grants["urgent"] = ResourceLease(
        "lease-urgent", "urgent", "crew", NOW
    )
    manager._pending_preemptions["urgent"] = ResourcePreemption(
        "lease-holder", "holder", "crew", NOW, preempted_by="urgent"
    )
    assert manager._try_commit_preemption("urgent") is False

    persistence = MemoryPersistence()
    demand = _demand("urgent", resource_name="crew", sequence=2)
    displaced = _reservation(
        "old-reservation", "holder", resource_name="other", sequence=1
    )
    persistence._state.preemptive_resource_demands["urgent"] = demand
    persistence._state.preemptive_resource_reservations[
        "old-reservation"
    ] = displaced
    manager = DurablePreemptiveResourceManager(persistence)
    manager._pending_grants["urgent"] = ResourceLease(
        "lease-urgent", "urgent", "crew", NOW
    )
    manager._pending_preemptions["urgent"] = ResourcePreemption(
        "lease-holder", "holder", "other", NOW, preempted_by="urgent"
    )

    with pytest.raises(RuntimeError, match="preemption pair crosses resources"):
        manager._try_commit_preemption("urgent")


@pytest.mark.parametrize("changed", ["demand", "reservation"])
def test_preemptive_pairing_loses_transaction_race(monkeypatch, changed):
    persistence = MemoryPersistence()
    demand, displaced, lease, event = _seed_preemption_pair(persistence)
    manager = DurablePreemptiveResourceManager(persistence)
    manager._pending_grants["urgent"] = lease
    manager._pending_preemptions["urgent"] = event

    class RacingUow:
        def get_preemptive_resource_demand(self, request_id):
            return None if changed == "demand" else demand

        def get_preemptive_resource_reservation(self, reservation_id):
            return None if changed == "reservation" else displaced

    @contextmanager
    def transaction():
        yield RacingUow()

    monkeypatch.setattr(persistence, "transaction", transaction)
    assert manager._try_commit_preemption("urgent") is False


def test_normal_grant_requires_both_lease_and_demand():
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    manager = DurablePreemptiveResourceManager(persistence)

    assert manager._try_commit_normal_grant("missing") is False

    manager._pending_grants["lease-only"] = ResourceLease(
        "lease-only", "lease-only", "crew", NOW
    )
    assert manager._try_commit_normal_grant("lease-only") is False

    persistence._state.preemptive_resource_demands["demand-only"] = _demand(
        "demand-only"
    )
    assert manager._try_commit_normal_grant("demand-only") is False


def test_normal_grant_returns_concurrent_reservation_or_rejects_lost_demand(
    monkeypatch,
):
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    demand = _demand("request")
    lease = ResourceLease("lease", "request", "crew", NOW)
    manager = DurablePreemptiveResourceManager(persistence)

    existing = _reservation(
        "existing-reservation", "request", resource_name="crew", sequence=1
    )
    persistence._state.preemptive_resource_reservations[
        existing.reservation_id
    ] = existing

    class LostDemandUow:
        def get_preemptive_resource_demand(self, request_id):
            return None

    @contextmanager
    def transaction():
        yield LostDemandUow()

    monkeypatch.setattr(persistence, "transaction", transaction)
    assert manager._commit_normal_grant(demand, lease) == existing

    persistence._state.preemptive_resource_reservations.clear()
    with pytest.raises(RuntimeError, match="demand changed before grant"):
        manager._commit_normal_grant(demand, lease)


def test_reconcile_pending_grants_skips_other_resources_and_completed_preemption(
    monkeypatch,
):
    persistence = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(persistence)
    manager._pending_grants["other"] = ResourceLease(
        "lease-other", "other", "other-resource", NOW
    )
    manager._pending_grants["done"] = ResourceLease(
        "lease-done", "done", "crew", NOW
    )

    attempted_normal = []
    monkeypatch.setattr(
        manager,
        "_try_commit_preemption",
        lambda request_id: request_id == "done",
    )
    monkeypatch.setattr(
        manager,
        "_try_commit_normal_grant",
        attempted_normal.append,
    )

    manager._reconcile_pending_grants("crew")
    assert attempted_normal == []


@pytest.mark.parametrize("changed", ["intent", "reservation"])
def test_finalize_release_detects_transaction_races(monkeypatch, changed):
    persistence = MemoryPersistence()
    manager = DurablePreemptiveResourceManager(persistence)
    reservation = _reservation()
    intent = PreemptiveResourceReleaseIntent(
        "release", reservation.reservation_id, "crew", NOW
    )

    class RacingReleaseUow:
        def get_preemptive_resource_release_intent(self, intent_id):
            return None if changed == "intent" else intent

        def get_preemptive_resource_reservation(self, reservation_id):
            return None if changed == "reservation" else reservation

    @contextmanager
    def transaction():
        yield RacingReleaseUow()

    monkeypatch.setattr(persistence, "transaction", transaction)
    message = (
        "release intent changed"
        if changed == "intent"
        else "reservation changed during release"
    )
    with pytest.raises(RuntimeError, match=message):
        manager._finalize_release_intent(intent, reservation)


def test_rebuild_cleanup_drops_stale_release_without_reservation():
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_release_intents["stale"] = (
        PreemptiveResourceReleaseIntent(
            "stale", "missing-reservation", "crew", NOW
        )
    )

    DurablePreemptiveResourceManager(
        persistence
    )._finalize_interrupted_releases()

    assert persistence.preemptive_resource_release_intents() == ()


def test_preemptive_callback_rejects_wrong_resource_and_unknown_definition():
    persistence = MemoryPersistence()
    persistence._state.preemptive_resource_definitions["crew"] = _definition()
    persistence._state.preemptive_resource_reservations["reservation"] = _reservation()
    manager = DurablePreemptiveResourceManager(persistence)

    with pytest.raises(RuntimeError, match="targets wrong resource"):
        manager._record_preempted(
            ResourcePreemption(
                "lease",
                "holder",
                "other",
                NOW,
                preempted_by="new",
            )
        )

    with pytest.raises(KeyError, match="unknown preemptive resource definition"):
        manager._definition("missing")

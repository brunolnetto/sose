from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
    ResourceDefinition,
    ResourceDemand,
    ResourcePreemptionResult,
    ResourceReleaseIntent,
    ResourceReservation,
    ScheduledWork,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)
from sose.persistence.memory import (
    MemoryPersistence,
    MemoryUnitOfWork,
    fork_state,
)


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=1)


def _item(
    item_id: str = "item",
    store_name: str = "store",
    value: object = "value",
    *,
    priority: int = 100,
    sequence: int = 1,
) -> DurableStoreItem:
    return DurableStoreItem(item_id, store_name, value, priority, sequence)


def _put(
    item_id: str = "item",
    store_name: str = "store",
    value: object = "value",
    *,
    priority: int = 100,
    sequence: int = 1,
) -> StorePutIntent:
    return StorePutIntent(
        item_id,
        store_name,
        value,
        priority,
        NOW,
        sequence,
    )


def _get(
    request_id: str = "get",
    store_name: str = "store",
    *,
    sequence: int = 1,
) -> StoreGetRequest:
    return StoreGetRequest(request_id, store_name, NOW, sequence)


def _get_result(
    request_id: str = "get",
    store_name: str = "store",
    *,
    value: object = "value",
    sequence: int = 1,
) -> StoreGetResult:
    return StoreGetResult(
        request_id,
        store_name,
        _item(store_name=store_name, value=value),
        NOW,
        sequence,
    )


def _container_intent(
    request_id: str = "op",
    container_name: str = "tank",
    operation: str = "put",
    amount: float = 1.0,
    *,
    sequence: int = 1,
) -> ContainerOperationIntent:
    return ContainerOperationIntent(
        request_id,
        container_name,
        operation,
        amount,
        NOW,
        sequence,
    )


def _container_result(
    request_id: str = "op",
    container_name: str = "tank",
    operation: str = "put",
    amount: float = 1.0,
    *,
    level_before: float = 1.0,
    level_after: float = 2.0,
    sequence: int = 1,
) -> ContainerOperationResult:
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


def _preemptive_demand(
    request_id: str = "request",
    resource_name: str = "crew",
    *,
    priority: int = 100,
    sequence: int = 1,
) -> PreemptiveResourceDemand:
    return PreemptiveResourceDemand(
        request_id,
        resource_name,
        priority,
        True,
        NOW,
        sequence,
    )


def _preemptive_reservation(
    reservation_id: str = "reservation",
    request_id: str = "request",
    resource_name: str = "crew",
    *,
    priority: int = 100,
    sequence: int = 1,
) -> PreemptiveResourceReservation:
    return PreemptiveResourceReservation(
        reservation_id,
        request_id,
        resource_name,
        NOW,
        priority,
        sequence,
    )


def test_job_domain_domain_delivery_and_scheduled_work_guards():
    persistence = MemoryPersistence()

    with persistence.transaction() as uow:
        uow.save_job_state(
            SimpleNamespace(job_id="job", domain_name="alpha")
        )
        uow.save_domain_delivery(
            SimpleNamespace(mutation_id="mutation", mutation="alpha")
        )

    with pytest.raises(ValueError, match="job domain cannot change"):
        with persistence.transaction() as uow:
            uow.save_job_state(
                SimpleNamespace(job_id="job", domain_name="beta")
            )

    with pytest.raises(ValueError, match="domain mutation identity conflict"):
        with persistence.transaction() as uow:
            uow.save_domain_delivery(
                SimpleNamespace(mutation_id="mutation", mutation="beta")
            )

    with pytest.raises(KeyError, match="unknown scheduled command"):
        with persistence.transaction() as uow:
            uow.save_scheduled_work(
                ScheduledWork("work", NOW, 100, 1, "missing-command")
            )


def test_resource_definition_demand_reservation_and_release_guards():
    persistence = MemoryPersistence()

    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("crew", 1))

    with pytest.raises(ValueError, match="resource definition already exists"):
        with persistence.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("crew", 2))

    with pytest.raises(KeyError, match="unknown resource definition"):
        with persistence.transaction() as uow:
            uow.save_resource_demand(
                ResourceDemand("missing", "unknown", 100, NOW, 1)
            )

    with persistence.transaction() as uow:
        uow.save_resource_reservation(
            ResourceReservation("reserved", "owned", "crew", NOW, 1)
        )

    with pytest.raises(ValueError, match="resource request already reserved"):
        with persistence.transaction() as uow:
            uow.save_resource_demand(
                ResourceDemand("owned", "crew", 100, NOW, 1)
            )

    with persistence.transaction() as uow:
        uow.save_resource_demand(
            ResourceDemand("pending", "crew", 100, NOW, 2)
        )

    with pytest.raises(ValueError, match="resource demand already exists"):
        with persistence.transaction() as uow:
            uow.save_resource_demand(
                ResourceDemand("pending", "crew", 50, NOW, 2)
            )

    with pytest.raises(KeyError, match="unknown resource definition"):
        with persistence.transaction() as uow:
            uow.save_resource_reservation(
                ResourceReservation("bad", "bad", "missing", NOW, 3)
            )

    with pytest.raises(ValueError, match="resource request is still pending"):
        with persistence.transaction() as uow:
            uow.save_resource_reservation(
                ResourceReservation("pending-res", "pending", "crew", NOW, 3)
            )

    with pytest.raises(ValueError, match="resource request already reserved"):
        with persistence.transaction() as uow:
            uow.save_resource_reservation(
                ResourceReservation("duplicate", "owned", "crew", NOW, 4)
            )

    with pytest.raises(KeyError, match="unknown resource reservation"):
        with persistence.transaction() as uow:
            uow.save_resource_release_intent(
                ResourceReleaseIntent("release-missing", "missing", "crew", NOW)
            )

    with persistence.transaction() as uow:
        uow.save_resource_release_intent(
            ResourceReleaseIntent("release", "reserved", "crew", NOW)
        )

    with pytest.raises(ValueError, match="resource release intent already exists"):
        with persistence.transaction() as uow:
            uow.save_resource_release_intent(
                ResourceReleaseIntent("release", "reserved", "crew", LATER)
            )


def test_store_unit_of_work_guards_and_idempotent_result_path():
    persistence = MemoryPersistence()

    with persistence.transaction() as uow:
        uow.save_store_definition(StoreDefinition("store"))

    with pytest.raises(ValueError, match="store definition already exists"):
        with persistence.transaction() as uow:
            uow.save_store_definition(StoreDefinition("store", kind="priority"))

    with pytest.raises(KeyError, match="unknown store definition"):
        with persistence.transaction() as uow:
            uow.save_store_item(_item(store_name="missing"))

    with persistence.transaction() as uow:
        uow.save_store_put_intent(_put("pending"))

    with pytest.raises(ValueError, match="still pending put"):
        with persistence.transaction() as uow:
            uow.save_store_item(_item("pending"))

    with persistence.transaction() as uow:
        uow.delete_store_put_intent("pending")
        uow.save_store_item(_item("accepted", value="one", sequence=2))

    with pytest.raises(ValueError, match="store item already exists"):
        with persistence.transaction() as uow:
            uow.save_store_item(_item("accepted", value="two", sequence=2))

    with pytest.raises(KeyError, match="unknown store definition"):
        with persistence.transaction() as uow:
            uow.save_store_put_intent(_put("missing", store_name="missing"))

    with pytest.raises(ValueError, match="already accepted"):
        with persistence.transaction() as uow:
            uow.save_store_put_intent(_put("accepted", sequence=2))

    with persistence.transaction() as uow:
        uow.save_store_put_intent(_put("intent", value="one", sequence=3))

    with pytest.raises(ValueError, match="store put intent already exists"):
        with persistence.transaction() as uow:
            uow.save_store_put_intent(_put("intent", value="two", sequence=3))

    with pytest.raises(KeyError, match="unknown store definition"):
        with persistence.transaction() as uow:
            uow.save_store_get_request(_get("unknown", "missing"))

    with persistence.transaction() as uow:
        uow.save_store_get_request(_get("completed", sequence=4))
        result = _get_result("completed", sequence=4)
        uow.save_store_get_result(result)
        uow.delete_store_get_request("completed")

    with pytest.raises(ValueError, match="already completed"):
        with persistence.transaction() as uow:
            uow.save_store_get_request(_get("completed", sequence=4))

    with persistence.transaction() as uow:
        uow.save_store_get_request(_get("request", sequence=5))

    with pytest.raises(ValueError, match="store get request already exists"):
        with persistence.transaction() as uow:
            changed = StoreGetRequest("request", "store", LATER, 5)
            uow.save_store_get_request(changed)

    with pytest.raises(KeyError, match="unknown store definition"):
        with persistence.transaction() as uow:
            uow.save_store_get_result(
                _get_result("bad-store", store_name="missing", sequence=6)
            )

    with persistence.transaction() as uow:
        assert uow.get_store_get_result("absent") is None
        assert uow.get_store_get_result("completed") == result
        # Replaying the exact result is idempotent.
        uow.save_store_get_result(result)

    with pytest.raises(ValueError, match="store get result already exists"):
        with persistence.transaction() as uow:
            uow.save_store_get_result(
                _get_result("completed", value="changed", sequence=4)
            )

    with pytest.raises(KeyError, match="unknown store get request"):
        with persistence.transaction() as uow:
            uow.save_store_get_result(
                _get_result("never-requested", sequence=7)
            )

    with persistence.transaction() as uow:
        uow.save_store_definition(StoreDefinition("other"))
        uow.save_store_get_request(_get("wrong-store", "store", sequence=8))

    with pytest.raises(ValueError, match="targets wrong store"):
        with persistence.transaction() as uow:
            uow.save_store_get_result(
                _get_result("wrong-store", store_name="other", sequence=8)
            )


def test_container_unit_of_work_guards_and_idempotent_result_path():
    persistence = MemoryPersistence()

    with persistence.transaction() as uow:
        uow.save_container_definition(ContainerDefinition("tank", 10.0, 1.0))

    with pytest.raises(ValueError, match="container definition already exists"):
        with persistence.transaction() as uow:
            uow.save_container_definition(ContainerDefinition("tank", 20.0, 1.0))

    with pytest.raises(KeyError, match="unknown container definition"):
        with persistence.transaction() as uow:
            uow.save_container_state(ContainerState("missing", 1.0))

    with pytest.raises(ValueError, match="container level exceeds capacity"):
        with persistence.transaction() as uow:
            uow.save_container_state(ContainerState("tank", 11.0))

    with pytest.raises(KeyError, match="unknown container definition"):
        with persistence.transaction() as uow:
            uow.save_container_operation_intent(
                _container_intent("unknown", container_name="missing")
            )

    with persistence.transaction() as uow:
        uow.save_container_operation_intent(_container_intent("completed"))
        result = _container_result("completed")
        uow.save_container_operation_result(result)
        uow.delete_container_operation_intent("completed")

    with pytest.raises(ValueError, match="container request already completed"):
        with persistence.transaction() as uow:
            uow.save_container_operation_intent(_container_intent("completed"))

    with persistence.transaction() as uow:
        uow.save_container_operation_intent(_container_intent("pending", sequence=2))

    with pytest.raises(ValueError, match="container request already exists"):
        with persistence.transaction() as uow:
            uow.save_container_operation_intent(
                _container_intent("pending", amount=2.0, sequence=2)
            )

    with persistence.transaction() as uow:
        uow.save_container_operation_result(result)

    with pytest.raises(ValueError, match="container result already exists"):
        with persistence.transaction() as uow:
            uow.save_container_operation_result(
                _container_result("completed", amount=2.0)
            )

    with pytest.raises(KeyError, match="unknown container operation intent"):
        with persistence.transaction() as uow:
            uow.save_container_operation_result(_container_result("missing"))

    with persistence.transaction() as uow:
        uow.save_container_operation_intent(
            _container_intent("mismatch", operation="put", sequence=3)
        )

    with pytest.raises(ValueError, match="does not match intent"):
        with persistence.transaction() as uow:
            uow.save_container_operation_result(
                _container_result(
                    "mismatch",
                    operation="get",
                    level_before=2.0,
                    level_after=1.0,
                    sequence=3,
                )
            )


def test_preemptive_unit_of_work_guards_and_preemption_result_invariants():
    persistence = MemoryPersistence()

    with persistence.transaction() as uow:
        uow.save_preemptive_resource_definition(
            PreemptiveResourceDefinition("crew", 1)
        )

    with pytest.raises(ValueError, match="preemptive resource definition already exists"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_definition(
                PreemptiveResourceDefinition("crew", 2)
            )

    with pytest.raises(KeyError, match="unknown preemptive resource definition"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_demand(
                _preemptive_demand("missing", "missing")
            )

    with persistence.transaction() as uow:
        uow.save_preemptive_resource_reservation(
            _preemptive_reservation("owned-res", "owned")
        )

    with pytest.raises(ValueError, match="preemptive resource request already reserved"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_demand(
                _preemptive_demand("owned", sequence=2)
            )

    with persistence.transaction() as uow:
        uow.save_preemptive_resource_demand(
            _preemptive_demand("pending", sequence=3)
        )

    with pytest.raises(ValueError, match="preemptive resource demand already exists"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_demand(
                _preemptive_demand("pending", priority=50, sequence=3)
            )

    with pytest.raises(KeyError, match="unknown preemptive resource definition"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_reservation(
                _preemptive_reservation("bad", "bad", "missing", sequence=4)
            )

    with pytest.raises(ValueError, match="preemptive resource request is still pending"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_reservation(
                _preemptive_reservation(
                    "pending-res",
                    "pending",
                    sequence=4,
                )
            )

    with pytest.raises(ValueError, match="preemptive resource request already reserved"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_reservation(
                _preemptive_reservation(
                    "duplicate-owned",
                    "owned",
                    sequence=5,
                )
            )

    with pytest.raises(KeyError, match="unknown preemptive resource reservation"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_release_intent(
                PreemptiveResourceReleaseIntent(
                    "release-missing",
                    "missing",
                    "crew",
                    NOW,
                )
            )

    with persistence.transaction() as uow:
        uow.save_preemptive_resource_release_intent(
            PreemptiveResourceReleaseIntent(
                "release",
                "owned-res",
                "crew",
                NOW,
            )
        )

    with pytest.raises(ValueError, match="preemptive resource release intent already exists"):
        with persistence.transaction() as uow:
            uow.save_preemptive_resource_release_intent(
                PreemptiveResourceReleaseIntent(
                    "release",
                    "owned-res",
                    "crew",
                    LATER,
                )
            )

    with persistence.transaction() as uow:
        assert uow.get_resource_preemption_result("missing") is None

    missing_successor = ResourcePreemptionResult(
        "result",
        "crew",
        "old-res",
        "old",
        "new",
        "missing-successor",
        NOW,
        1,
    )
    with pytest.raises(KeyError, match="unknown successor preemptive reservation"):
        with persistence.transaction() as uow:
            uow.save_resource_preemption_result(missing_successor)

    with persistence.transaction() as uow:
        uow.save_preemptive_resource_reservation(
            _preemptive_reservation(
                "successor",
                "successor-request",
                sequence=6,
            )
        )

    wrong_successor = ResourcePreemptionResult(
        "wrong-successor",
        "crew",
        "old-res",
        "old",
        "different-request",
        "successor",
        NOW,
        2,
    )
    with pytest.raises(ValueError, match="successor does not match request"):
        with persistence.transaction() as uow:
            uow.save_resource_preemption_result(wrong_successor)

    displaced_active = ResourcePreemptionResult(
        "displaced-active",
        "crew",
        "owned-res",
        "owned",
        "successor-request",
        "successor",
        NOW,
        3,
    )
    with pytest.raises(ValueError, match="displaced reservation is still active"):
        with persistence.transaction() as uow:
            uow.save_resource_preemption_result(displaced_active)

    valid = ResourcePreemptionResult(
        "valid-result",
        "crew",
        "gone-res",
        "gone",
        "successor-request",
        "successor",
        NOW,
        4,
    )
    with persistence.transaction() as uow:
        uow.save_resource_preemption_result(valid)
        assert uow.get_resource_preemption_result("valid-result") == valid

    changed = ResourcePreemptionResult(
        "valid-result",
        "crew",
        "different-gone-res",
        "gone",
        "successor-request",
        "successor",
        NOW,
        4,
    )
    with pytest.raises(ValueError, match="resource preemption result already exists"):
        with persistence.transaction() as uow:
            uow.save_resource_preemption_result(changed)


def test_memory_commit_rejects_pending_completed_overlaps():
    persistence = MemoryPersistence()

    store_uow = MemoryUnitOfWork(fork_state(persistence._state), persistence)
    store_uow._working.store_get_requests["same"] = _get("same")
    store_uow._working.store_get_results["same"] = _get_result("same")
    with pytest.raises(ValueError, match="store get identity cannot be pending and completed"):
        store_uow.commit()

    container_uow = MemoryUnitOfWork(fork_state(persistence._state), persistence)
    container_uow._working.container_operation_intents["same"] = _container_intent(
        "same"
    )
    container_uow._working.container_operation_results["same"] = _container_result(
        "same"
    )
    with pytest.raises(ValueError, match="container operation identity cannot be pending and completed"):
        container_uow.commit()


def test_manual_rollback_closes_transaction_without_publishing_working_state():
    persistence = MemoryPersistence()

    with persistence.transaction() as uow:
        uow.save_store_definition(StoreDefinition("rolled-back"))
        assert ("store_definitions", "rolled-back") in uow.dirty_records
        uow.rollback()

    assert persistence.store_definitions() == ()

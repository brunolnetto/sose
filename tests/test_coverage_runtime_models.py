from __future__ import annotations

from datetime import datetime, timezone
import math

import pytest

from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
    DurableStoreItem,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
    ResourceDefinition,
    ResourceDemand,
    ResourcePreemptionResult,
    SimulationPosition,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"logical_tick": -1}, "logical_tick must be >= 0"),
        ({"execution_sequence": -1}, "execution_sequence must be >= 0"),
        ({"committed_sequence": -1}, "committed_sequence must be >= 0"),
        (
            {"execution_sequence": 1, "committed_sequence": 2},
            "committed_sequence cannot exceed",
        ),
    ],
)
def test_simulation_position_validates_sequences(kwargs, message):
    values = dict(
        logical_time=NOW,
        execution_sequence=2,
        committed_sequence=1,
        logical_tick=1,
    )
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        SimulationPosition(**values)
    assert SimulationPosition(NOW, 2, 1, 1).logical_tick == 1


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: ResourceDefinition("", 1), "resource name cannot be empty"),
        (lambda: ResourceDefinition("r", 0), "capacity must be >= 1"),
        (
            lambda: ResourceDemand("", "r", 0, NOW, 0),
            "request_id cannot be empty",
        ),
        (
            lambda: ResourceDemand("req", "", 0, NOW, 0),
            "resource_name cannot be empty",
        ),
        (lambda: StoreDefinition(""), "store name cannot be empty"),
        (
            lambda: StoreDefinition("s", kind="bad"),
            "store kind must be one of",
        ),
        (
            lambda: StoreDefinition("s", capacity=0),
            "store capacity must be >= 1",
        ),
        (
            lambda: DurableStoreItem("", "s", "v", 0, 1),
            "item_id cannot be empty",
        ),
        (
            lambda: DurableStoreItem("i", "", "v", 0, 1),
            "store_name cannot be empty",
        ),
        (
            lambda: DurableStoreItem("i", "s", "v", 0, 0),
            "store item sequence must be >= 1",
        ),
        (
            lambda: StorePutIntent("", "s", "v", 0, NOW, 1),
            "item_id cannot be empty",
        ),
        (
            lambda: StorePutIntent("i", "", "v", 0, NOW, 1),
            "store_name cannot be empty",
        ),
        (
            lambda: StorePutIntent("i", "s", "v", 0, NOW, 0),
            "store put sequence must be >= 1",
        ),
        (
            lambda: StoreGetRequest("", "s", NOW, 1),
            "request_id cannot be empty",
        ),
        (
            lambda: StoreGetRequest("r", "", NOW, 1),
            "store_name cannot be empty",
        ),
        (
            lambda: StoreGetRequest("r", "s", NOW, 0),
            "store get sequence must be >= 1",
        ),
    ],
)
def test_resource_and_store_models_validate_identity_and_sequences(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def _item(store="s") -> DurableStoreItem:
    return DurableStoreItem("i", store, "value", 0, 1)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: StoreGetResult("", "s", _item(), NOW, 1),
            "request_id cannot be empty",
        ),
        (
            lambda: StoreGetResult("r", "", _item(), NOW, 1),
            "store_name cannot be empty",
        ),
        (
            lambda: StoreGetResult("r", "s", _item("other"), NOW, 1),
            "different store",
        ),
        (
            lambda: StoreGetResult("r", "s", _item(), NOW, 0),
            "result sequence must be >= 1",
        ),
        (
            lambda: PreemptiveResourceDefinition("", 1),
            "resource name cannot be empty",
        ),
        (
            lambda: PreemptiveResourceDefinition("r", 0),
            "capacity must be >= 1",
        ),
        (
            lambda: PreemptiveResourceDemand("", "r", 0, True, NOW, 1),
            "request_id cannot be empty",
        ),
        (
            lambda: PreemptiveResourceDemand("req", "", 0, True, NOW, 1),
            "resource_name cannot be empty",
        ),
        (
            lambda: PreemptiveResourceDemand("req", "r", 0, True, NOW, 0),
            "demand sequence must be >= 1",
        ),
        (
            lambda: PreemptiveResourceReservation("", "req", "r", NOW, 0, 1),
            "reservation_id cannot be empty",
        ),
        (
            lambda: PreemptiveResourceReservation("res", "", "r", NOW, 0, 1),
            "request_id cannot be empty",
        ),
        (
            lambda: PreemptiveResourceReservation("res", "req", "", NOW, 0, 1),
            "resource_name cannot be empty",
        ),
        (
            lambda: PreemptiveResourceReservation("res", "req", "r", NOW, 0, 0),
            "reservation sequence must be >= 1",
        ),
    ],
)
def test_get_and_preemptive_resource_models_validate_contract(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def _preemption(**overrides) -> ResourcePreemptionResult:
    values = dict(
        result_id="result",
        resource_name="resource",
        displaced_reservation_id="old-res",
        displaced_request_id="old-req",
        preempting_request_id="new-req",
        successor_reservation_id="new-res",
        preempted_at=NOW,
        sequence=1,
    )
    values.update(overrides)
    return ResourcePreemptionResult(**values)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"result_id": ""}, "result_id cannot be empty"),
        ({"resource_name": ""}, "resource_name cannot be empty"),
        (
            {"displaced_reservation_id": ""},
            "displaced_reservation_id cannot be empty",
        ),
        ({"displaced_request_id": ""}, "displaced_request_id cannot be empty"),
        ({"preempting_request_id": ""}, "preempting_request_id cannot be empty"),
        (
            {"successor_reservation_id": ""},
            "successor_reservation_id cannot be empty",
        ),
        ({"sequence": 0}, "result sequence must be >= 1"),
    ],
)
def test_preemption_result_validates_all_identity_fields(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _preemption(**kwargs)
    assert _preemption().sequence == 1


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: ContainerDefinition("", 1),
            "container name cannot be empty",
        ),
        (
            lambda: ContainerDefinition("c", 0),
            "capacity must be finite and > 0",
        ),
        (
            lambda: ContainerDefinition("c", math.inf),
            "capacity must be finite and > 0",
        ),
        (
            lambda: ContainerDefinition("c", 10, initial=-1),
            "initial level must be finite",
        ),
        (
            lambda: ContainerDefinition("c", 10, initial=11),
            "initial level must be finite",
        ),
        (
            lambda: ContainerDefinition("c", 10, initial=math.nan),
            "initial level must be finite",
        ),
        (
            lambda: ContainerState("", 0),
            "container name cannot be empty",
        ),
        (
            lambda: ContainerState("c", -1),
            "level must be finite and >= 0",
        ),
        (
            lambda: ContainerState("c", math.inf),
            "level must be finite and >= 0",
        ),
        (
            lambda: ContainerOperationIntent("", "c", "put", 1, NOW, 1),
            "request_id cannot be empty",
        ),
        (
            lambda: ContainerOperationIntent("r", "", "put", 1, NOW, 1),
            "container_name cannot be empty",
        ),
        (
            lambda: ContainerOperationIntent("r", "c", "bad", 1, NOW, 1),
            "operation must be put or get",
        ),
        (
            lambda: ContainerOperationIntent("r", "c", "put", 0, NOW, 1),
            "amount must be finite and > 0",
        ),
        (
            lambda: ContainerOperationIntent("r", "c", "put", math.nan, NOW, 1),
            "amount must be finite and > 0",
        ),
        (
            lambda: ContainerOperationIntent("r", "c", "put", 1, NOW, 0),
            "operation sequence must be >= 1",
        ),
    ],
)
def test_container_definition_state_and_intent_validation(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


def _container_result(**overrides) -> ContainerOperationResult:
    values = dict(
        request_id="r",
        container_name="c",
        operation="put",
        amount=1.0,
        completed_at=NOW,
        level_before=1.0,
        level_after=2.0,
        sequence=1,
    )
    values.update(overrides)
    return ContainerOperationResult(**values)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"request_id": ""}, "request_id cannot be empty"),
        ({"container_name": ""}, "container_name cannot be empty"),
        ({"operation": "bad"}, "operation must be put or get"),
        ({"amount": 0}, "amount must be finite and > 0"),
        ({"amount": math.inf}, "amount must be finite and > 0"),
        ({"level_before": -1}, "levels must be finite and >= 0"),
        ({"level_after": -1}, "levels must be finite and >= 0"),
        ({"level_before": math.nan}, "levels must be finite and >= 0"),
        ({"sequence": 0}, "result sequence must be >= 1"),
    ],
)
def test_container_operation_result_validates_contract(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _container_result(**kwargs)
    assert _container_result().level_after == 2.0

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from sose.organizational.agency import (
    A1Policy,
    A1PolicyName,
    A2ObservationModel,
    AgencyLevel,
    AgencySpec,
)


def test_a0_has_no_adaptive_policy() -> None:
    spec = AgencySpec(level=AgencyLevel.A0)
    assert spec.a1_policy is None
    assert spec.a2_observation is None


def test_a1_requires_named_policy_from_closed_catalog() -> None:
    spec = AgencySpec(
        level=AgencyLevel.A1,
        a1_policy=A1Policy(name=A1PolicyName.QUEUE_BYPASS, parameters={"threshold": 3}),
    )
    assert spec.a1_policy is not None
    assert spec.a1_policy.name is A1PolicyName.QUEUE_BYPASS
    with pytest.raises(ValidationError):
        A1Policy.model_validate({"name": "invented_after_results", "parameters": {}})


def test_a1_rejects_missing_or_extraneous_policy() -> None:
    with pytest.raises(ValidationError, match="A1 requires"):
        AgencySpec(level=AgencyLevel.A1)
    with pytest.raises(ValidationError, match="only valid for A1"):
        AgencySpec(level=AgencyLevel.A0, a1_policy=A1Policy(name=A1PolicyName.BATCHING))


def test_a1_parameters_are_deeply_immutable_and_json_safe() -> None:
    policy = A1Policy(name=A1PolicyName.BATCHING, parameters={"size": 3, "labels": ["a"]})
    with pytest.raises(TypeError):
        policy.parameters["size"] = 4  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        policy.parameters["labels"].append("b")  # type: ignore[index,union-attr]
    with pytest.raises(ValidationError):
        A1Policy(name=A1PolicyName.BATCHING, parameters={"bad": object()})
    with pytest.raises(ValidationError):
        A1Policy(name=A1PolicyName.BATCHING, parameters={"bad": math.inf})


def test_a2_requires_observation_model_and_permitted_actions() -> None:
    observation = A2ObservationModel(
        metrics=("lead_time_p95", "wip"),
        aggregation_window=24.0,
        observation_delay=2.0,
        measurement_noise_std=0.1,
        trigger_rule="lead_time_p95 > 48",
        cooldown=12.0,
        permitted_actions=("set_wip_limit", "activate_overtime"),
    )
    spec = AgencySpec(level=AgencyLevel.A2, a2_observation=observation)
    assert spec.a2_observation == observation
    with pytest.raises(ValidationError, match="A2 requires"):
        AgencySpec(level=AgencyLevel.A2)


def test_a2_observation_model_rejects_oracle_like_empty_contract() -> None:
    with pytest.raises(ValidationError):
        A2ObservationModel(
            metrics=(), aggregation_window=1, trigger_rule="always", permitted_actions=("set_wip_limit",)
        )
    with pytest.raises(ValidationError):
        A2ObservationModel(
            metrics=("wip",), aggregation_window=1, trigger_rule="wip > 5", permitted_actions=()
        )


@pytest.mark.parametrize(
    ("metrics", "trigger", "actions"),
    [
        (("",), "wip > 5", ("set_wip_limit",)),
        (("   ",), "wip > 5", ("set_wip_limit",)),
        (("wip",), "   ", ("set_wip_limit",)),
        (("wip",), "wip > 5", ("",)),
        (("wip",), "wip > 5", ("   ",)),
    ],
)
def test_a2_rejects_blank_metric_trigger_and_action_names(
    metrics: tuple[str, ...], trigger: str, actions: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        A2ObservationModel(
            metrics=metrics,
            aggregation_window=1,
            trigger_rule=trigger,
            permitted_actions=actions,
        )


@pytest.mark.parametrize(
    "field_name",
    ["aggregation_window", "observation_delay", "measurement_noise_std", "cooldown"],
)
def test_a2_rejects_non_finite_numeric_observation_parameters(field_name: str) -> None:
    payload: dict[str, object] = {
        "metrics": ("wip",),
        "aggregation_window": 1.0,
        "observation_delay": 0.0,
        "measurement_noise_std": 0.0,
        "trigger_rule": "wip > 5",
        "cooldown": 0.0,
        "permitted_actions": ("set_wip_limit",),
    }
    payload[field_name] = math.inf
    with pytest.raises(ValidationError):
        A2ObservationModel.model_validate(payload)


def test_a3_is_explicitly_out_of_model() -> None:
    with pytest.raises(ValidationError, match="A3 is outside"):
        AgencySpec(level=AgencyLevel.A3)

"""Prospective v2 delay-analog and PR-case models (split from prospective_stage1_fit_v2)."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .model_spec import EvidenceClass, ModelSpec


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DelayAnalogV2(BaseModel):
    """One training-only item-delay analog.

    Workflow-active time is directly observed machine activity. The remaining
    elapsed time is deliberately causal-subtype agnostic: it is not actor effort.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_active_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    unidentified_residual_seconds: float = Field(ge=0.0, allow_inf_nan=False)

    @property
    def lead_time_seconds(self) -> float:
        return self.workflow_active_seconds + self.unidentified_residual_seconds


class PRReviewV2TailModel(BaseModel):
    """Frozen empirical analog model for prospective PR lead-time prediction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_snapshot_hash: NonBlankString
    human_analogs: tuple[DelayAnalogV2, ...] = Field(min_length=1)
    bot_analogs: tuple[DelayAnalogV2, ...] = ()

    def analog_pool(self, *, author_is_bot: bool) -> tuple[DelayAnalogV2, ...]:
        if author_is_bot and self.bot_analogs:
            return self.bot_analogs
        return self.human_analogs

    def build_model_spec(self) -> ModelSpec:
        return ModelSpec(
            stations={
                "workflow_activity": {
                    "kind": "observed-machine-delay-distribution",
                    "accounting": "union-active-time",
                },
                "unidentified_elapsed_delay": {
                    "kind": "item-delay-distribution",
                    "causal_subtype": "unidentified",
                },
            },
            routing={
                "item_flow": [
                    "workflow_activity",
                    "unidentified_elapsed_delay",
                    "complete",
                ]
            },
            policies={
                "analog_sampling": "paired-empirical-with-replacement",
                "stratification": "author_is_bot_at_item_creation",
                "bot_pool_fallback": "human_pool_if_training_has_no_bot_items",
            },
            parameters={
                "human_delay_analog_pairs": [
                    [
                        analog.workflow_active_seconds,
                        analog.unidentified_residual_seconds,
                    ]
                    for analog in self.human_analogs
                ],
                "bot_delay_analog_pairs": [
                    [
                        analog.workflow_active_seconds,
                        analog.unidentified_residual_seconds,
                    ]
                    for analog in self.bot_analogs
                ],
                "bot_stratification": True,
            },
            parameter_evidence={
                "human_delay_analog_pairs": EvidenceClass.INFERABLE,
                "bot_delay_analog_pairs": EvidenceClass.INFERABLE,
                "bot_stratification": EvidenceClass.OBSERVED,
            },
        )


class PRReviewV2Case(BaseModel):
    """Prediction-time inputs available when the pull request is created."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_id: NonBlankString
    opened_at: float = Field(ge=0.0, allow_inf_nan=False)
    author_is_bot: bool


class PRReviewV2Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lead_time_seconds: dict[str, float]
    completed_at: dict[str, float]

from __future__ import annotations

from hashlib import sha256
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .empirical_pilot import GitHubPRObservationSnapshot, PRReviewEmpiricalPilotResult, run_pr_review_empirical_pilot
from .heldout_prediction import PRReviewAssumptions
from .validation import LeadTimeValidationCriteria

PLAN_VERSION = "pr-review-empirical-plan/v1"
EXECUTION_VERSION = "pr-review-preregistered-execution/v1"
SplitPolicy = Literal["chronological-purged/v1"]


class PRReviewEmpiricalPlan(BaseModel):
    """Inputs fixed before evaluating a PR-review empirical holdout.

    A plan hash provides deterministic identity. It does not by itself prove that
    the plan was published or timestamped before the holdout outcome was observed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["pr-review-empirical-plan/v1"] = PLAN_VERSION
    snapshot_hash: str = Field(min_length=1)
    split_policy: SplitPolicy = "chronological-purged/v1"
    holdout_fraction: float = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    assumptions: PRReviewAssumptions
    validation_criteria: LeadTimeValidationCriteria
    seed: int

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @property
    def plan_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


class PRReviewPreregisteredPilotResult(BaseModel):
    """Auditable binding from a preregistration plan to its empirical result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_version: Literal["pr-review-preregistered-execution/v1"] = EXECUTION_VERSION
    plan: PRReviewEmpiricalPlan
    plan_hash: str = Field(min_length=1)
    result: PRReviewEmpiricalPilotResult

    @model_validator(mode="after")
    def validate_plan_result_binding(self) -> "PRReviewPreregisteredPilotResult":
        if self.plan_hash != self.plan.plan_hash:
            raise ValueError("plan hash does not match preregistration plan")
        if self.result.snapshot_hash != self.plan.snapshot_hash:
            raise ValueError("result snapshot hash does not match plan snapshot hash")
        if self.result.holdout_fraction != self.plan.holdout_fraction:
            raise ValueError("result holdout fraction does not match plan")
        if self.result.assumptions != self.plan.assumptions:
            raise ValueError("result assumptions do not match plan")
        if self.result.validation_criteria != self.plan.validation_criteria:
            raise ValueError("result validation criteria do not match plan")
        if self.result.seed != self.plan.seed:
            raise ValueError("result seed does not match plan")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @property
    def execution_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def execute_pr_review_empirical_plan(*, snapshot: GitHubPRObservationSnapshot, plan: PRReviewEmpiricalPlan) -> PRReviewPreregisteredPilotResult:
    """Execute exactly the inputs declared by a preregistered plan."""

    if snapshot.snapshot_hash != plan.snapshot_hash:
        raise ValueError("snapshot hash does not match preregistered plan")
    if plan.split_policy != "chronological-purged/v1":
        raise ValueError(f"unsupported split policy: {plan.split_policy}")
    result = run_pr_review_empirical_pilot(
        snapshot=snapshot,
        holdout_fraction=plan.holdout_fraction,
        assumptions=plan.assumptions,
        validation_criteria=plan.validation_criteria,
        seed=plan.seed,
    )
    return PRReviewPreregisteredPilotResult(plan=plan, plan_hash=plan.plan_hash, result=result)

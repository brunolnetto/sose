from __future__ import annotations

import json
from pathlib import Path

from sose.organizational.empirical_pilot import GitHubPRObservationSnapshot
from sose.organizational.offline_runner import run_pr_review_empirical_files
from sose.organizational.preregistration import (
    PRReviewEmpiricalPlan,
    PRReviewPreregisteredPilotResult,
)


ROOT = Path(__file__).resolve().parents[4]
DOCS = ROOT / "docs" / "organizational"
PROTOCOL = DOCS / "pr-review-validation-preregistration-v1.json"
SNAPSHOT = DOCS / "pr-review-validation-source-v1.json"
PLAN = DOCS / "pr-review-validation-plan-v1.json"
RESULT = DOCS / "pr-review-validation-result-v1.json"
EXPECTED_SNAPSHOT_HASH = "339c5df588e5afac6788712ba21bbb516f8569dcd139e49cfbde4a325ae140b5"
EXPECTED_PLAN_HASH = "dc886cf185f51e675542fbba1111c19bb421cdb0c755eecf4a74056c4ba77142"
EXPECTED_EXECUTION_HASH = "406143ff97a17cf89b78ac92e2d081d7866d361e388055c106a5df8c62345d2d"


def test_frozen_validation_artifacts_execute_protocol_without_retuning(tmp_path: Path) -> None:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    snapshot = GitHubPRObservationSnapshot.model_validate_json(
        SNAPSHOT.read_text(encoding="utf-8")
    )
    plan = PRReviewEmpiricalPlan.model_validate_json(PLAN.read_text(encoding="utf-8"))
    persisted = PRReviewPreregisteredPilotResult.model_validate_json(
        RESULT.read_text(encoding="utf-8")
    )

    assert snapshot.snapshot_hash == EXPECTED_SNAPSHOT_HASH
    assert plan.plan_hash == EXPECTED_PLAN_HASH
    assert persisted.execution_hash == EXPECTED_EXECUTION_HASH
    assert [record.pr_number for record in snapshot.records] == protocol["source_pr_numbers"]
    assert plan.holdout_fraction == protocol["holdout_fraction"]
    assert plan.seed == protocol["seed"]
    assert plan.assumptions.model_dump(mode="json") == protocol["assumptions"]
    assert plan.validation_criteria.model_dump(mode="json") == protocol["validation_criteria"]

    expected_train = tuple((protocol["repository"], number) for number in protocol["expected_train_pr_numbers"])
    expected_holdout = tuple((protocol["repository"], number) for number in protocol["expected_holdout_pr_numbers"])
    expected_purged = tuple((protocol["repository"], number) for number in protocol["expected_purged_pr_numbers"])

    assert persisted.result.train_keys == expected_train
    assert persisted.result.holdout_keys == expected_holdout
    assert persisted.result.purged_keys == expected_purged

    output = tmp_path / "execution.json"
    rerun = run_pr_review_empirical_files(
        snapshot_path=SNAPSHOT,
        plan_path=PLAN,
        output_path=output,
    )

    assert rerun == persisted
    assert rerun.execution_hash == EXPECTED_EXECUTION_HASH
    assert output.read_bytes() == RESULT.read_bytes()


def test_first_frozen_holdout_result_is_retained_even_when_gate_fails() -> None:
    execution = PRReviewPreregisteredPilotResult.model_validate_json(
        RESULT.read_text(encoding="utf-8")
    )

    assessment = execution.result.validation_assessment
    checks = {check.metric: check for check in assessment.checks}

    assert assessment.passed is False
    assert checks["abs_mean_difference_seconds"].passed is False
    assert checks["abs_median_difference_seconds"].passed is True
    assert checks["abs_p90_difference_seconds"].passed is True
    assert checks["ecdf_max_distance"].passed is True

    # The preregistration explicitly retains outliers and forbids post-hoc holdout removal.
    assert execution.result.validation_assessment.observed_dataset_hash == execution.result.holdout_dataset_hash
    assert len(execution.result.holdout_keys) == 12

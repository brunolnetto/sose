from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
PROTOCOL = ROOT / "docs" / "organizational" / "pr-review-validation-preregistration-v2.json"


def test_v2_is_prospective_two_stage_and_does_not_reuse_v1_holdout() -> None:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))

    assert protocol["protocol_version"] == "pr-review-validation/v2"
    assert protocol["study_type"] == "prospective_two_stage"
    assert protocol["repository"] == "brunolnetto/sose"
    assert protocol["registration_pr_number"] == 301
    assert protocol["activation_rule"] == "pull requests created strictly after registration PR merge"

    cohort = protocol["cohort"]
    assert cohort["training_count"] == 18
    assert cohort["holdout_count"] == 12
    assert cohort["selection"] == "first eligible merged pull requests in creation-time order"
    assert cohort["v1_source_prs_excluded"] == list(range(265, 291))

    source = protocol["source_contract"]
    assert source["workflow_jobs_included"] is True
    assert source["review_timeline_included"] is True
    assert source["submitted_reviews_included"] is True
    assert source["infer_actor_effort_from_timestamp_gaps"] is False

    staging = protocol["stage_rules"]
    assert staging["holdout_enrollment_before_model_freeze"] is False
    assert staging["v2_model_may_use_training_only"] is True
    assert staging["holdout_outcomes_may_change_model"] is False
    assert staging["failed_holdout_must_be_reported"] is True

    rules = protocol["analysis_rules"]
    assert rules["retain_outliers"] is True
    assert rules["exclude_holdout_items_posthoc"] is False
    assert rules["retune_after_holdout"] is False
    assert rules["composite_fit_score"] is False


def test_v2_requires_tail_mechanisms_to_be_supported_by_observed_evidence() -> None:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))

    hypotheses = protocol["candidate_tail_mechanisms"]
    by_name = {entry["mechanism"]: entry for entry in hypotheses}

    assert by_name["ci_wait_or_execution"]["evidence"] == "workflow jobs"
    assert by_name["review_response_wait"]["evidence"] == "review request/submission timestamps"
    assert by_name["calendar_or_off_hours_wait"]["evidence_class"] == "inferable"
    assert by_name["human_service_effort"]["evidence_class"] == "assumed"

    assert protocol["model_freeze_requirements"]["mechanism_without_source_evidence"] == (
        "remain assumed/swept or stay out of v2"
    )

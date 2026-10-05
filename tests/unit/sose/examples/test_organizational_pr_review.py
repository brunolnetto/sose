from __future__ import annotations

import pytest
from pydantic import ValidationError

from sose.core.randomness import CounterRandomSource
from sose.examples.organizational_pr_review import (
    PullRequestCase,
    PullRequestFlowConfig,
    build_model_spec,
    simulate_pull_request_flow,
)
from sose.organizational.ledger import ActorCategory, ItemCategory
from sose.organizational.model_spec import EvidenceClass


def _cases() -> tuple[PullRequestCase, ...]:
    return (
        PullRequestCase(pr_id="pr-1", opened_at=0.0),
        PullRequestCase(pr_id="pr-2", opened_at=0.0),
        PullRequestCase(pr_id="pr-3", opened_at=0.5),
    )


def _config(*, reviewer_count: int = 1, rework_probability: float = 0.0) -> PullRequestFlowConfig:
    return PullRequestFlowConfig(
        reviewer_count=reviewer_count,
        ci_time=1.0,
        mean_review_time=2.0,
        mean_revision_time=1.0,
        rework_probability=rework_probability,
    )


def test_config_and_case_reject_invalid_flow_inputs() -> None:
    with pytest.raises(ValidationError):
        PullRequestFlowConfig(
            reviewer_count=0,
            ci_time=1.0,
            mean_review_time=2.0,
            mean_revision_time=1.0,
            rework_probability=0.0,
        )
    with pytest.raises(ValidationError):
        PullRequestFlowConfig(
            reviewer_count=1,
            ci_time=0.0,
            mean_review_time=2.0,
            mean_revision_time=1.0,
            rework_probability=0.0,
        )
    with pytest.raises(ValidationError):
        PullRequestFlowConfig(
            reviewer_count=1,
            ci_time=1.0,
            mean_review_time=2.0,
            mean_revision_time=1.0,
            rework_probability=1.0,
        )
    with pytest.raises(ValidationError):
        PullRequestCase(pr_id="", opened_at=0.0)
    with pytest.raises(ValidationError):
        PullRequestCase(pr_id="pr", opened_at=-1.0)


def test_simulation_rejects_empty_and_duplicate_case_sets() -> None:
    with pytest.raises(ValueError, match="at least one pull request"):
        simulate_pull_request_flow(cases=(), config=_config(), seed=1)
    duplicate = (
        PullRequestCase(pr_id="pr-1", opened_at=0.0),
        PullRequestCase(pr_id="pr-1", opened_at=1.0),
    )
    with pytest.raises(ValueError, match="duplicate pull request"):
        simulate_pull_request_flow(cases=duplicate, config=_config(), seed=1)


def test_model_spec_separates_observed_flow_inputs_from_assumed_human_effort() -> None:
    spec = build_model_spec(_config())

    assert spec.stations["ci"]["kind"] == "automation"
    assert spec.stations["review"]["capacity"] == 1
    assert spec.parameter_evidence["ci_time"] is EvidenceClass.OBSERVED
    assert spec.parameter_evidence["mean_review_time"] is EvidenceClass.ASSUMED
    assert spec.parameter_evidence["mean_revision_time"] is EvidenceClass.ASSUMED
    assert spec.parameter_evidence["rework_probability"] is EvidenceClass.INFERABLE


def test_same_seed_produces_identical_run_and_complete_item_ledgers() -> None:
    left = simulate_pull_request_flow(cases=_cases(), config=_config(), seed=20261005)
    right = simulate_pull_request_flow(cases=_cases(), config=_config(), seed=20261005)

    assert left == right
    for case in _cases():
        ledger = left.item_ledger(case.pr_id)
        ledger.assert_complete(
            created_at=case.opened_at,
            observed_at=left.completed_at[case.pr_id],
        )


def test_single_reviewer_creates_explicit_queue_time() -> None:
    result = simulate_pull_request_flow(cases=_cases(), config=_config(), seed=7)

    queued = result.item_ledger("pr-2").duration_by_primary().get(ItemCategory.QUEUE, 0.0)
    assert queued > 0.0


def test_more_review_capacity_changes_queueing_not_logical_service_draws() -> None:
    one = simulate_pull_request_flow(cases=_cases(), config=_config(reviewer_count=1), seed=17)
    two = simulate_pull_request_flow(cases=_cases(), config=_config(reviewer_count=2), seed=17)

    for pr_id in ("pr-1", "pr-2", "pr-3"):
        one_review = one.item_ledger(pr_id).duration_by_primary()[ItemCategory.COORDINATION]
        two_review = two.item_ledger(pr_id).duration_by_primary()[ItemCategory.COORDINATION]
        assert one_review == pytest.approx(two_review)

    assert two.mean_lead_time < one.mean_lead_time
    assert sum(
        two.item_ledger(pr_id).duration_by_primary().get(ItemCategory.QUEUE, 0.0)
        for pr_id in two.completed_at
    ) < sum(
        one.item_ledger(pr_id).duration_by_primary().get(ItemCategory.QUEUE, 0.0)
        for pr_id in one.completed_at
    )


def test_rework_is_explicit_and_consumes_author_actor_time() -> None:
    probability = 0.5
    seed = next(
        candidate
        for candidate in range(10_000)
        if CounterRandomSource(candidate).bernoulli(
            probability,
            stream="rework",
            entity_id="pr-1",
            mechanism="review_outcome",
            draw_index=0,
        )
        and not CounterRandomSource(candidate).bernoulli(
            probability,
            stream="rework",
            entity_id="pr-1",
            mechanism="review_outcome",
            draw_index=1,
        )
    )
    result = simulate_pull_request_flow(
        cases=(PullRequestCase(pr_id="pr-1", opened_at=0.0),),
        config=_config(rework_probability=probability),
        seed=seed,
    )

    durations = result.item_ledger("pr-1").duration_by_primary()
    assert result.review_cycles["pr-1"] == 2
    assert durations[ItemCategory.REWORK] > 0.0

    author = result.actor_ledger("author:pr-1")
    assert author.duration_by_category()[ActorCategory.EXECUTION] == pytest.approx(
        durations[ItemCategory.REWORK]
    )


def test_queue_elapsed_time_does_not_consume_reviewer_actor_time() -> None:
    result = simulate_pull_request_flow(cases=_cases(), config=_config(), seed=31)

    total_item_review = sum(
        result.item_ledger(pr_id).duration_by_primary().get(ItemCategory.COORDINATION, 0.0)
        for pr_id in result.completed_at
    )
    total_queue = sum(
        result.item_ledger(pr_id).duration_by_primary().get(ItemCategory.QUEUE, 0.0)
        for pr_id in result.completed_at
    )
    reviewer_actor_time = sum(
        result.actor_ledger(reviewer_id).allocated_actor_time
        for reviewer_id in result.reviewer_ids
    )

    assert total_queue > 0.0
    assert reviewer_actor_time == pytest.approx(total_item_review)


def test_result_reports_flow_observables_without_invented_productivity_index() -> None:
    result = simulate_pull_request_flow(cases=_cases(), config=_config(), seed=41)

    assert result.mean_lead_time > 0.0
    assert result.peak_wip == 3
    assert result.throughput > 0.0
    assert not hasattr(result, "organizational_efficiency")

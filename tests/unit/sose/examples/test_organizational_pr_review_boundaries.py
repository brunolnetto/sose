from __future__ import annotations

from sose.examples.organizational_pr_review import (
    PullRequestCase,
    PullRequestFlowConfig,
    simulate_pull_request_flow,
)


def test_completion_and_open_at_same_instant_do_not_create_false_wip_spike() -> None:
    config = PullRequestFlowConfig(
        reviewer_count=1,
        ci_time=1.0,
        mean_review_time=2.0,
        mean_revision_time=1.0,
        rework_probability=0.0,
    )
    seed = 123
    first = simulate_pull_request_flow(
        cases=(PullRequestCase(pr_id="pr-1", opened_at=0.0),),
        config=config,
        seed=seed,
    )
    boundary = first.completed_at["pr-1"]

    result = simulate_pull_request_flow(
        cases=(
            PullRequestCase(pr_id="pr-1", opened_at=0.0),
            PullRequestCase(pr_id="pr-2", opened_at=boundary),
        ),
        config=config,
        seed=seed,
    )

    assert result.completed_at["pr-1"] == boundary
    assert result.peak_wip == 1

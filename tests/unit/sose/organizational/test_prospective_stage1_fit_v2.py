from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sose.organizational.model_spec import EvidenceClass
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage1_fit_v2 import (
    REQUIRED_STAGE2_TAIL_METRICS_V2,
    PRReviewV2Case,
    derive_stage2_acceptance_criteria_v2,
    fit_stage1_pr_review_v2,
    predict_pr_review_v2,
    workflow_active_seconds_v2,
)
from sose.organizational.prospective_state_v2 import advance_prospective_evidence_state_v2
from sose.organizational.source_evidence_v2 import (
    GitHubPREvidenceRecordV2,
    GitHubWorkflowJobSourceRecordV2,
)


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)


def test_workflow_activity_unions_overlapping_jobs_without_double_counting() -> None:
    record = _record(
        302,
        opened_offset=10,
        lead_time=100,
        jobs=((10, 50), (30, 80), (90, 130)),
    )

    # [10, 80] plus clipped [90, 110] = 90 seconds, not 140.
    assert workflow_active_seconds_v2(record) == 90.0


def test_stage1_fit_preserves_tail_as_unidentified_residual_not_actor_effort() -> None:
    state = _state(
        _record(302, opened_offset=10, lead_time=100, jobs=((10, 40),)),
        _record(303, opened_offset=20, lead_time=900, jobs=((20, 50),)),
        _record(
            304,
            opened_offset=30,
            lead_time=7_500,
            jobs=((30, 60),),
            author_is_bot=True,
        ),
    )

    fit = fit_stage1_pr_review_v2(
        state=state,
        simulation_seed=41,
        acceptance_bootstrap_seed=43,
        acceptance_bootstrap_replicates=128,
        acceptance_quantile=0.95,
    )

    human_residuals = tuple(
        analog.unidentified_residual_seconds for analog in fit.model.human_analogs
    )
    assert 70.0 in human_residuals
    assert 870.0 in human_residuals
    assert tuple(
        analog.unidentified_residual_seconds for analog in fit.model.bot_analogs
    ) == (7_470.0,)
    assert fit.model_spec.parameter_evidence["human_delay_analog_pairs"] is EvidenceClass.INFERABLE
    assert fit.model_spec.parameter_evidence["bot_delay_analog_pairs"] is EvidenceClass.INFERABLE
    assert fit.model_spec.parameter_evidence["bot_stratification"] is EvidenceClass.OBSERVED
    assert "reviewer_service_time" not in fit.model_spec.parameters
    assert "reviewer_capacity" not in fit.model_spec.parameters
    assert fit.tail_metrics == REQUIRED_STAGE2_TAIL_METRICS_V2


def test_prediction_is_counter_keyed_and_conditions_only_on_creation_time_bot_flag() -> None:
    state = _state(
        _record(302, opened_offset=10, lead_time=100, jobs=((10, 40),)),
        _record(303, opened_offset=20, lead_time=200, jobs=((20, 50),)),
        _record(
            304,
            opened_offset=30,
            lead_time=7_500,
            jobs=((30, 60),),
            author_is_bot=True,
        ),
    )
    fit = fit_stage1_pr_review_v2(
        state=state,
        simulation_seed=41,
        acceptance_bootstrap_seed=43,
        acceptance_bootstrap_replicates=64,
        acceptance_quantile=0.95,
    )
    cases = (
        PRReviewV2Case(pr_id="future-human", opened_at=0.0, author_is_bot=False),
        PRReviewV2Case(pr_id="future-bot", opened_at=1.0, author_is_bot=True),
    )

    first = predict_pr_review_v2(cases=cases, model=fit.model, seed=fit.simulation_seed)
    second = predict_pr_review_v2(cases=tuple(reversed(cases)), model=fit.model, seed=fit.simulation_seed)

    assert first.lead_time_seconds["future-human"] in {100.0, 200.0}
    assert first.lead_time_seconds["future-bot"] == 7_500.0
    assert first.lead_time_seconds == second.lead_time_seconds


def test_acceptance_criteria_are_training_only_deterministic_and_non_composite() -> None:
    state = _state(
        _record(302, opened_offset=10, lead_time=100, jobs=((10, 40),)),
        _record(303, opened_offset=20, lead_time=200, jobs=((20, 50),)),
        _record(304, opened_offset=30, lead_time=400, jobs=((30, 60),)),
        _record(
            305,
            opened_offset=40,
            lead_time=2_000,
            jobs=((40, 70),),
            author_is_bot=True,
        ),
    )
    fit = fit_stage1_pr_review_v2(
        state=state,
        simulation_seed=41,
        acceptance_bootstrap_seed=43,
        acceptance_bootstrap_replicates=128,
        acceptance_quantile=0.95,
    )

    independently_derived = derive_stage2_acceptance_criteria_v2(
        model=fit.model,
        training_bot_fraction=fit.training_bot_fraction,
        seed=43,
        replicates=128,
        quantile=0.95,
    )

    assert fit.acceptance_criteria == independently_derived
    assert fit.acceptance_criteria.criteria_hash == independently_derived.criteria_hash
    assert fit.acceptance_criteria.max_abs_mean_difference_seconds >= 0.0
    assert fit.acceptance_criteria.max_abs_median_difference_seconds >= 0.0
    assert fit.acceptance_criteria.max_abs_p90_difference_seconds >= 0.0
    assert 0.0 <= fit.acceptance_criteria.max_ecdf_distance <= 1.0


def test_fit_uses_only_persisted_training_keys_not_interstitial_items() -> None:
    protocol = _protocol()
    first = advance_prospective_evidence_state_v2(
        records=_training_records(
            _record(302, opened_offset=10, lead_time=100, jobs=((10, 40),)),
            _record(303, opened_offset=20, lead_time=200, jobs=((20, 50),)),
        ),
        protocol=protocol,
    )
    state = advance_prospective_evidence_state_v2(
        records=(
            *first.snapshot.records,
            _record(400, opened_offset=10_000, lead_time=9_000, jobs=((10_000, 10_030),)),
        ),
        protocol=protocol,
        previous_state=first,
    )

    fit = fit_stage1_pr_review_v2(
        state=state,
        simulation_seed=41,
        acceptance_bootstrap_seed=43,
        acceptance_bootstrap_replicates=64,
        acceptance_quantile=0.95,
    )

    assert fit.training_keys == first.cohort.training_keys
    assert len(fit.model.human_analogs) == 18
    assert all(analog.lead_time_seconds != 9_000.0 for analog in fit.model.human_analogs)


def _state(*records: GitHubPREvidenceRecordV2):
    return advance_prospective_evidence_state_v2(
        records=_training_records(*records),
        protocol=_protocol(),
    )


def _training_records(
    *records: GitHubPREvidenceRecordV2,
) -> tuple[GitHubPREvidenceRecordV2, ...]:
    supplied = {record.pr_number for record in records}
    filler: list[GitHubPREvidenceRecordV2] = []
    candidate = 500
    while len(records) + len(filler) < 18:
        while candidate in supplied:
            candidate += 1
        offset = 20_000 + len(filler) * 200
        filler.append(
            _record(
                candidate,
                opened_offset=offset,
                lead_time=100,
                jobs=((offset, offset + 30),),
            )
        )
        candidate += 1
    return (*records, *filler)


def _protocol() -> ProspectiveStudyProtocolV2:
    return ProspectiveStudyProtocolV2(
        protocol_document_hash="0" * 64,
        registration_merged_at=REGISTERED_AT,
    )


def _record(
    pr_number: int,
    *,
    opened_offset: int,
    lead_time: int,
    jobs: tuple[tuple[int, int], ...],
    author_is_bot: bool = False,
) -> GitHubPREvidenceRecordV2:
    opened_at = REGISTERED_AT + timedelta(seconds=opened_offset)
    return GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=pr_number,
        opened_at=opened_at,
        merged_at=opened_at + timedelta(seconds=lead_time),
        source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{pr_number}",
        author_actor_key=("renovate[bot]" if author_is_bot else "brunolnetto"),
        author_is_bot=author_is_bot,
        workflow_jobs=tuple(
            GitHubWorkflowJobSourceRecordV2(
                job_id=pr_number * 100 + index,
                workflow_id=1,
                run_id=pr_number * 10 + index,
                run_attempt=1,
                name=f"job-{index}",
                started_at=REGISTERED_AT + timedelta(seconds=start),
                completed_at=REGISTERED_AT + timedelta(seconds=end),
                conclusion="success",
                source_url=f"https://api.github.com/jobs/{pr_number}-{index}",
            )
            for index, (start, end) in enumerate(jobs, start=1)
        ),
    )

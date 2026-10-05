from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.examples.organizational_pr_review import PullRequestFlowEvidence
from sose.organizational.dataset import ObservedPRDataset, ObservedPRSplit
from sose.organizational.heldout_prediction import (
    PRReviewAssumptions,
    predict_pr_review_holdout,
)
from sose.organizational.model_spec import EvidenceClass
from sose.organizational.observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(
    event_id: str,
    kind: ObservedEventKind,
    hour: float,
    *,
    gate: bool = False,
) -> ObservedPREvent:
    return ObservedPREvent(
        source_event_id=event_id,
        kind=kind,
        occurred_at=BASE + timedelta(hours=hour),
        metadata={"ci_gate": True, "ci_gate_evidence": "config://required-check"} if gate else {},
    )


def _trace(
    number: int,
    *,
    opened_hour: float,
    terminal_hour: float,
    ci_start: float | None = None,
    ci_end: float | None = None,
    ci_gate: bool = False,
) -> ObservedPRTrace:
    events = [_event(f"{number}:open", ObservedEventKind.OPENED, opened_hour)]
    if ci_start is not None:
        events.append(
            _event(f"{number}:ci-start", ObservedEventKind.CI_STARTED, ci_start, gate=ci_gate)
        )
    if ci_end is not None:
        events.append(
            _event(f"{number}:ci-end", ObservedEventKind.CI_COMPLETED, ci_end, gate=ci_gate)
        )
    events.append(_event(f"{number}:merged", ObservedEventKind.MERGED, terminal_hour))
    return ObservedPRTrace(repository="example/repo", pr_number=number, events=tuple(events))


def _split(*, holdout_terminal_shift: float = 0.0, identified_gate: bool = True) -> ObservedPRSplit:
    train = ObservedPRDataset(
        dataset_version="train-v1",
        traces=(
            _trace(1, opened_hour=0, terminal_hour=4, ci_start=0.25, ci_end=0.75, ci_gate=identified_gate),
            _trace(2, opened_hour=2, terminal_hour=7, ci_start=2.25, ci_end=3.25, ci_gate=identified_gate),
            _trace(3, opened_hour=5, terminal_hour=9, ci_start=5.25, ci_end=6.0, ci_gate=identified_gate),
        ),
    )
    holdout = ObservedPRDataset(
        dataset_version="holdout-v1",
        traces=(
            _trace(4, opened_hour=12, terminal_hour=16 + holdout_terminal_shift),
            _trace(5, opened_hour=13, terminal_hour=18 + holdout_terminal_shift),
        ),
    )
    return ObservedPRSplit(train=train, holdout=holdout)


def _assumptions() -> PRReviewAssumptions:
    return PRReviewAssumptions(
        reviewer_count=2,
        mean_review_time_seconds=1800.0,
        mean_revision_time_seconds=900.0,
        rework_probability=0.2,
        fallback_ci_time_seconds=1200.0,
    )


def test_prediction_calibrates_only_from_training_data_and_binds_both_hashes() -> None:
    split = _split()
    result = predict_pr_review_holdout(split=split, assumptions=_assumptions(), seed=17)
    assert result.train_dataset_hash == split.train.dataset_hash
    assert result.holdout_dataset_hash == split.holdout.dataset_hash
    assert result.calibration.observed_dataset_hash == split.train.dataset_hash
    assert result.validation.observed_dataset_hash == split.holdout.dataset_hash
    assert len(result.simulated_lead_times_seconds) == len(split.holdout.traces)
    assert result.model_spec_hash


def test_changing_only_holdout_outcomes_cannot_change_calibration_or_predictions() -> None:
    baseline = predict_pr_review_holdout(split=_split(), assumptions=_assumptions(), seed=23)
    changed_holdout = predict_pr_review_holdout(
        split=_split(holdout_terminal_shift=24.0), assumptions=_assumptions(), seed=23
    )
    assert changed_holdout.train_dataset_hash == baseline.train_dataset_hash
    assert changed_holdout.calibration == baseline.calibration
    assert changed_holdout.model_spec_hash == baseline.model_spec_hash
    assert changed_holdout.simulated_lead_times_seconds == baseline.simulated_lead_times_seconds
    assert changed_holdout.holdout_dataset_hash != baseline.holdout_dataset_hash
    assert changed_holdout.validation != baseline.validation


def test_identified_training_ci_gate_is_used_as_observed_but_actor_parameters_remain_assumed() -> None:
    result = predict_pr_review_holdout(split=_split(), assumptions=_assumptions(), seed=31)
    assert result.config.ci_time == pytest.approx(2700.0)
    assert result.evidence.ci_time is EvidenceClass.OBSERVED
    assert result.evidence.reviewer_count is EvidenceClass.ASSUMED
    assert result.evidence.mean_review_time is EvidenceClass.ASSUMED
    assert result.evidence.mean_revision_time is EvidenceClass.ASSUMED
    assert result.evidence.rework_probability is EvidenceClass.ASSUMED


def test_unidentified_ci_activity_uses_fallback_even_when_job_duration_is_observed() -> None:
    result = predict_pr_review_holdout(
        split=_split(identified_gate=False), assumptions=_assumptions(), seed=32
    )
    assert result.calibration.ci_duration_seconds is not None
    assert result.calibration.ci_gate_duration_seconds is None
    assert result.config.ci_time == pytest.approx(_assumptions().fallback_ci_time_seconds)
    assert result.evidence.ci_time is EvidenceClass.ASSUMED


def test_parallel_training_gate_jobs_use_per_pr_gate_latency_not_mean_job_duration() -> None:
    train_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=1,
        events=(
            _event("1:open", ObservedEventKind.OPENED, 0),
            _event("1:ci-start-a", ObservedEventKind.CI_STARTED, 0.25, gate=True),
            _event("1:ci-start-b", ObservedEventKind.CI_STARTED, 0.25, gate=True),
            _event("1:ci-end-a", ObservedEventKind.CI_COMPLETED, 0.25 + 1 / 60, gate=True),
            _event("1:ci-end-b", ObservedEventKind.CI_COMPLETED, 0.75, gate=True),
            _event("1:merged", ObservedEventKind.MERGED, 2),
        ),
    )
    train = ObservedPRDataset(dataset_version="train-v1", traces=(train_trace,))
    split = ObservedPRSplit(train=train, holdout=_split().holdout)
    result = predict_pr_review_holdout(split=split, assumptions=_assumptions(), seed=37)
    assert result.calibration.ci_duration_seconds is not None
    assert result.calibration.ci_duration_seconds.mean == pytest.approx(15.5 * 60)
    assert result.calibration.ci_gate_duration_seconds is not None
    assert result.config.ci_time == pytest.approx(30 * 60)


def test_missing_training_ci_evidence_uses_declared_fallback_and_marks_it_assumed() -> None:
    train = ObservedPRDataset(
        dataset_version="train-v1",
        traces=(
            _trace(1, opened_hour=0, terminal_hour=4),
            _trace(2, opened_hour=2, terminal_hour=7),
        ),
    )
    split = ObservedPRSplit(train=train, holdout=_split().holdout)
    result = predict_pr_review_holdout(split=split, assumptions=_assumptions(), seed=41)
    assert result.config.ci_time == pytest.approx(_assumptions().fallback_ci_time_seconds)
    assert result.evidence.ci_time is EvidenceClass.ASSUMED


def test_same_training_seed_and_assumptions_are_deterministic() -> None:
    left = predict_pr_review_holdout(split=_split(), assumptions=_assumptions(), seed=51)
    right = predict_pr_review_holdout(split=_split(), assumptions=_assumptions(), seed=51)
    assert left == right


def test_holdout_arrival_schedule_is_preserved_without_using_terminal_outcomes_as_inputs() -> None:
    result = predict_pr_review_holdout(split=_split(), assumptions=_assumptions(), seed=61)
    assert result.holdout_case_opened_seconds == (0.0, 3600.0)
    assert all(value > 0.0 for value in result.simulated_lead_times_seconds)


def test_split_boundary_rejects_nonterminal_holdout_before_prediction() -> None:
    open_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=99,
        events=(_event("99:open", ObservedEventKind.OPENED, 20),),
    )
    bad_holdout = ObservedPRDataset(dataset_version="bad", traces=(open_trace,))
    with pytest.raises(ValueError, match="holdout traces must be terminal"):
        ObservedPRSplit(train=_split().train, holdout=bad_holdout)


def test_prediction_rejects_closed_abandoned_outcomes_not_modeled_by_canonical() -> None:
    closed_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=99,
        events=(
            _event("99:open", ObservedEventKind.OPENED, 20),
            _event("99:closed", ObservedEventKind.CLOSED, 22),
        ),
    )
    closed_holdout = ObservedPRDataset(dataset_version="closed", traces=(closed_trace,))
    split = ObservedPRSplit(train=_split().train, holdout=closed_holdout)
    with pytest.raises(ValueError, match="merged-only"):
        predict_pr_review_holdout(split=split, assumptions=_assumptions(), seed=71)


def test_prediction_rejects_closed_training_outcomes_too() -> None:
    closed_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=98,
        events=(
            _event("98:open", ObservedEventKind.OPENED, 0),
            _event("98:closed", ObservedEventKind.CLOSED, 2),
        ),
    )
    closed_train = ObservedPRDataset(dataset_version="closed-train", traces=(closed_trace,))
    split = ObservedPRSplit(train=closed_train, holdout=_split().holdout)
    with pytest.raises(ValueError, match="merged-only"):
        predict_pr_review_holdout(split=split, assumptions=_assumptions(), seed=73)


def test_canonical_evidence_defaults_remain_backward_compatible() -> None:
    evidence = PullRequestFlowEvidence()
    assert evidence.reviewer_count is EvidenceClass.OBSERVED
    assert evidence.ci_time is EvidenceClass.OBSERVED
    assert evidence.mean_review_time is EvidenceClass.ASSUMED
    assert evidence.mean_revision_time is EvidenceClass.ASSUMED
    assert evidence.rework_probability is EvidenceClass.INFERABLE

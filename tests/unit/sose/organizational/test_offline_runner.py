from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Thread

import pytest

from sose.organizational.empirical_pilot import GitHubPRObservationSnapshot, GitHubPRSourceRecord
from sose.organizational.heldout_prediction import PRReviewAssumptions
from sose.organizational.offline_runner import run_pr_review_empirical_files
from sose.organizational.preregistration import PRReviewEmpiricalPlan, PRReviewPreregisteredPilotResult
from sose.organizational.validation import LeadTimeValidationCriteria


UTC = timezone.utc
REPOSITORY = "brunolnetto/sose"


def _dt(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, second, tzinfo=UTC)


def _record(number: int, opened: datetime, merged: datetime) -> GitHubPRSourceRecord:
    return GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=number,
        opened_at=opened,
        merged_at=merged,
        source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{number}",
    )


def _snapshot() -> GitHubPRObservationSnapshot:
    return GitHubPRObservationSnapshot(
        snapshot_version="offline-fixture/v1",
        records=(
            _record(1, _dt(10, 0), _dt(10, 10)),
            _record(2, _dt(10, 20), _dt(10, 40)),
            _record(3, _dt(11, 0), _dt(11, 30)),
            _record(4, _dt(12, 0), _dt(12, 25)),
        ),
    )


def _plan(snapshot: GitHubPRObservationSnapshot, *, seed: int = 20261006) -> PRReviewEmpiricalPlan:
    return PRReviewEmpiricalPlan(
        snapshot_hash=snapshot.snapshot_hash,
        split_policy="chronological-purged/v1",
        holdout_fraction=0.25,
        assumptions=PRReviewAssumptions(
            reviewer_count=1,
            mean_review_time_seconds=300.0,
            mean_revision_time_seconds=180.0,
            rework_probability=0.10,
            fallback_ci_time_seconds=120.0,
        ),
        validation_criteria=LeadTimeValidationCriteria(
            max_abs_mean_difference_seconds=100_000.0,
            max_abs_median_difference_seconds=100_000.0,
            max_abs_p90_difference_seconds=100_000.0,
            max_ecdf_distance=1.0,
        ),
        seed=seed,
    )


def _write_inputs(
    tmp_path: Path, *, seed: int = 20261006, suffix: str = ""
) -> tuple[Path, Path, GitHubPRObservationSnapshot, PRReviewEmpiricalPlan]:
    snapshot = _snapshot()
    plan = _plan(snapshot, seed=seed)
    snapshot_path = tmp_path / f"snapshot{suffix}.json"
    plan_path = tmp_path / f"plan{suffix}.json"
    snapshot_path.write_text(snapshot.canonical_json(), encoding="utf-8")
    plan_path.write_text(plan.canonical_json(), encoding="utf-8")
    return snapshot_path, plan_path, snapshot, plan


def test_offline_runner_consumes_only_serialized_snapshot_and_plan(tmp_path: Path) -> None:
    snapshot_path, plan_path, snapshot, plan = _write_inputs(tmp_path)
    output_path = tmp_path / "execution.json"

    execution = run_pr_review_empirical_files(
        snapshot_path=snapshot_path,
        plan_path=plan_path,
        output_path=output_path,
    )

    assert execution.plan_hash == plan.plan_hash
    assert execution.result.snapshot_hash == snapshot.snapshot_hash
    assert output_path.read_text(encoding="utf-8") == execution.canonical_json() + "\n"


def test_offline_runner_is_byte_deterministic_for_same_inputs(tmp_path: Path) -> None:
    snapshot_path, plan_path, _, _ = _write_inputs(tmp_path)
    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"

    first = run_pr_review_empirical_files(
        snapshot_path=snapshot_path,
        plan_path=plan_path,
        output_path=first_output,
    )
    second = run_pr_review_empirical_files(
        snapshot_path=snapshot_path,
        plan_path=plan_path,
        output_path=second_output,
    )

    assert first.execution_hash == second.execution_hash
    assert first_output.read_bytes() == second_output.read_bytes()


def test_offline_runner_is_idempotent_when_existing_output_matches(tmp_path: Path) -> None:
    snapshot_path, plan_path, _, _ = _write_inputs(tmp_path)
    output_path = tmp_path / "execution.json"

    first = run_pr_review_empirical_files(
        snapshot_path=snapshot_path,
        plan_path=plan_path,
        output_path=output_path,
    )
    second = run_pr_review_empirical_files(
        snapshot_path=snapshot_path,
        plan_path=plan_path,
        output_path=output_path,
    )

    assert first == second
    assert output_path.read_text(encoding="utf-8") == first.canonical_json() + "\n"


def test_offline_runner_refuses_to_overwrite_different_artifact(tmp_path: Path) -> None:
    snapshot_path, plan_path, _, _ = _write_inputs(tmp_path)
    output_path = tmp_path / "execution.json"
    output_path.write_text('{"unrelated":true}\n', encoding="utf-8")

    with pytest.raises(FileExistsError, match="different artifact"):
        run_pr_review_empirical_files(
            snapshot_path=snapshot_path,
            plan_path=plan_path,
            output_path=output_path,
        )


def test_concurrent_different_runs_never_overwrite_each_other(tmp_path: Path) -> None:
    snapshot_path, first_plan_path, _, _ = _write_inputs(tmp_path, seed=20261006, suffix="-a")
    _, second_plan_path, _, _ = _write_inputs(tmp_path, seed=20261007, suffix="-b")
    output_path = tmp_path / "execution.json"
    start = Barrier(2)
    successes: list[PRReviewPreregisteredPilotResult] = []
    errors: list[BaseException] = []

    def invoke(plan_path: Path) -> None:
        try:
            start.wait()
            successes.append(
                run_pr_review_empirical_files(
                    snapshot_path=snapshot_path,
                    plan_path=plan_path,
                    output_path=output_path,
                )
            )
        except BaseException as exc:  # capture thread failure for the assertion below
            errors.append(exc)

    first = Thread(target=invoke, args=(first_plan_path,))
    second = Thread(target=invoke, args=(second_plan_path,))
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], FileExistsError)
    persisted = PRReviewPreregisteredPilotResult.model_validate_json(
        output_path.read_text(encoding="utf-8")
    )
    assert persisted.execution_hash == successes[0].execution_hash


def test_offline_runner_rejects_plan_bound_to_different_snapshot(tmp_path: Path) -> None:
    snapshot_path, plan_path, _, plan = _write_inputs(tmp_path)
    forged = plan.model_copy(update={"snapshot_hash": "different-snapshot"})
    plan_path.write_text(forged.canonical_json(), encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot hash"):
        run_pr_review_empirical_files(
            snapshot_path=snapshot_path,
            plan_path=plan_path,
            output_path=tmp_path / "execution.json",
        )


def test_offline_runner_does_not_create_output_when_input_is_invalid(tmp_path: Path) -> None:
    snapshot_path, plan_path, _, _ = _write_inputs(tmp_path)
    plan_path.write_text("not-json", encoding="utf-8")
    output_path = tmp_path / "execution.json"

    with pytest.raises(ValueError):
        run_pr_review_empirical_files(
            snapshot_path=snapshot_path,
            plan_path=plan_path,
            output_path=output_path,
        )

    assert not output_path.exists()

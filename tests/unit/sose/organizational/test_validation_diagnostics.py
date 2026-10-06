from __future__ import annotations

from pathlib import Path

import pytest

from sose.organizational.diagnostics import diagnose_pr_review_validation
from sose.organizational.empirical_pilot import GitHubPRObservationSnapshot
from sose.organizational.preregistration import PRReviewPreregisteredPilotResult


ROOT = Path(__file__).resolve().parents[4]
DOCS = ROOT / "docs" / "organizational"
SNAPSHOT = DOCS / "pr-review-validation-source-v1.json"
RESULT = DOCS / "pr-review-validation-result-v1.json"


def _artifacts() -> tuple[GitHubPRObservationSnapshot, PRReviewPreregisteredPilotResult]:
    return (
        GitHubPRObservationSnapshot.model_validate_json(SNAPSHOT.read_text(encoding="utf-8")),
        PRReviewPreregisteredPilotResult.model_validate_json(RESULT.read_text(encoding="utf-8")),
    )


def test_diagnostic_describes_tail_mismatch_without_changing_acceptance() -> None:
    snapshot, execution = _artifacts()

    diagnostic = diagnose_pr_review_validation(snapshot=snapshot, execution=execution)

    assert diagnostic.acceptance_unchanged is True
    assert diagnostic.original_gate_passed is False
    assert diagnostic.failing_metrics == ("abs_mean_difference_seconds",)
    assert diagnostic.largest_observed_key == ("brunolnetto/sose", 287)
    assert diagnostic.largest_observed_lead_time_seconds == pytest.approx(35_316.0)
    assert diagnostic.observed_max_share_of_total_lead_time == pytest.approx(0.8502708559046587)
    assert diagnostic.observed_max_to_median_ratio == pytest.approx(70.28059701492538)
    assert diagnostic.simulated_max_to_median_ratio == pytest.approx(1.4476541538026242)
    assert diagnostic.max_tail_gap_seconds == pytest.approx(34_548.91667581366)


def test_diagnostic_rejects_snapshot_from_another_execution() -> None:
    snapshot, execution = _artifacts()
    changed = snapshot.model_copy(update={"snapshot_version": "different-source/v1"})

    with pytest.raises(ValueError, match="snapshot hash"):
        diagnose_pr_review_validation(snapshot=changed, execution=execution)

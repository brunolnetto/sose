from __future__ import annotations

import pytest

from sose.organizational.synthetic_reference_experiment import (
    run_reference_synthetic_experiment_v1,
)

pytestmark = pytest.mark.slow


def test_reference_experiment_runs_and_binds_report_to_dataset() -> None:
    result = run_reference_synthetic_experiment_v1()

    assert result.dataset.replications == 16
    assert len(result.dataset.runs) == len(result.design.worlds) * 16
    assert result.report.protocol_hash == result.design.protocol.protocol_hash
    assert result.report.dataset_hash == result.dataset.dataset_hash
    assert result.report.world_count == len(result.design.worlds)
    assert result.report.stable_world_count > 0
    assert result.report.saturated_world_count > 0
    assert 0.0 <= result.report.direction_recovery_rate <= 1.0
    assert result.report_hash == result.report.report_hash

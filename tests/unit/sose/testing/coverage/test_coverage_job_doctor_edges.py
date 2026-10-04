from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sose.core.runtime import SimulationPosition
from sose.jobs.config import SOSEConfig
from sose.jobs.doctor import inspect_open_job_health
from sose.jobs.model import SimulationJobState


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config() -> SOSEConfig:
    return SOSEConfig.model_validate(
        {
            "domain": {"name": "tutorial_job"},
            "persistence": {"adapter": "memory"},
            "job": {"id": "doctor-edge"},
        }
    )


def _state(**overrides) -> SimulationJobState:
    values = dict(
        job_id="doctor-edge",
        domain_name="tutorial_job",
        config_json='{"random_seed": 1}',
        config_revision=1,
        status="ready",
        initialized=True,
        logical_time=NOW,
        next_tick=2,
    )
    values.update(overrides)
    return SimulationJobState(**values)


def test_inspect_open_job_health_reports_pending_sink_and_missing_position(
    monkeypatch,
):
    from sose.jobs import doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(issues=()),
    )

    class _Persistence:
        def job_state(self, job_id):
            return _state()

        def sink_deliveries(self, *, job_id):
            return (
                SimpleNamespace(
                    status="pending",
                    delivery_id="d1",
                    sink_name="lakehouse",
                    last_error=None,
                ),
                SimpleNamespace(
                    status="pending",
                    delivery_id="d2",
                    sink_name="lakehouse",
                    last_error="network timeout",
                ),
            )

        def simulation_position(self):
            return None

    report = inspect_open_job_health(
        _config(),
        persistence=_Persistence(),
        resolved_config_json='{"random_seed": 1}',
    )

    by_code = {issue.code: issue for issue in report.issues}
    assert "sink.delivery_pending" in by_code
    assert "job.position_missing" in by_code
    assert any("network timeout" in issue.message for issue in report.issues)


def test_inspect_open_job_health_reports_tick_and_time_mismatch(monkeypatch):
    from sose.jobs import doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(issues=()),
    )

    class _Persistence:
        def job_state(self, job_id):
            return _state(next_tick=3)

        def sink_deliveries(self, *, job_id):
            return ()

        def simulation_position(self):
            return SimulationPosition(
                logical_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                execution_sequence=3,
                committed_sequence=3,
                logical_tick=2,
            )

    report = inspect_open_job_health(
        _config(),
        persistence=_Persistence(),
        resolved_config_json='{"random_seed": 1}',
    )

    codes = {issue.code for issue in report.issues}
    assert "job.tick_mismatch" in codes
    assert "job.time_mismatch" in codes


def test_inspect_open_job_health_reports_invalid_config_failed_status_and_trigger(
    monkeypatch,
):
    from sose.jobs import doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(issues=()),
    )

    class _Persistence:
        def job_state(self, job_id):
            return _state(
                config_json="{broken",
                status="failed",
                last_error=None,
                active_trigger_id="scheduler-1",
                phase="reconcile",
            )

        def sink_deliveries(self, *, job_id):
            return ()

        def simulation_position(self):
            return SimulationPosition(
                logical_time=NOW,
                execution_sequence=2,
                committed_sequence=2,
                logical_tick=2,
            )

    report = inspect_open_job_health(
        _config(),
        persistence=_Persistence(),
        resolved_config_json='{"random_seed": 1}',
    )

    by_code = {issue.code: issue for issue in report.issues}
    assert "job.config_invalid" in by_code
    assert by_code["job.failed"].message == "job is in failed state"
    assert "job.trigger_unresolved" in by_code


def test_inspect_open_job_health_reports_domain_mismatch(monkeypatch):
    from sose.jobs import doctor as doctor_module

    monkeypatch.setattr(
        doctor_module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(issues=()),
    )

    class _Persistence:
        def job_state(self, job_id):
            return _state(domain_name="different-domain")

        def sink_deliveries(self, *, job_id):
            return ()

        def simulation_position(self):
            return SimulationPosition(
                logical_time=NOW,
                execution_sequence=1,
                committed_sequence=1,
                logical_tick=2,
            )

    report = inspect_open_job_health(
        _config(),
        persistence=_Persistence(),
        resolved_config_json='{"random_seed": 1}',
    )

    codes = {issue.code for issue in report.issues}
    assert "job.domain_mismatch" in codes

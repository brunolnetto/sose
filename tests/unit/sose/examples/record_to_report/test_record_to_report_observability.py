from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import subprocess
import sys

from sose.backends.simpy import SimPyBackend
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.examples.record_to_report.config import RecordToReportConfig
from sose.examples.record_to_report.definition import definition
from sose.examples.record_to_report.observability import (
    record_to_report_kpis,
    record_to_report_projection,
)
from sose.examples.record_to_report.process_audit import process_manifest
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_close,
    reopen_period,
    run_adjustment_path,
    run_happy_path,
    schedule_close,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_r2r_projection_is_read_only_and_idempotent() -> None:
    persistence, entities = run_happy_path()
    period = persistence.entity("accounting_period", entities.period_id)
    assert period is not None
    before_version = period.version

    left = record_to_report_projection(persistence, entities=entities)
    right = record_to_report_projection(persistence, entities=entities)

    assert left == right
    assert left.period_id == entities.period_id
    assert left.period_state == "closed"
    assert left.journal_state == "posted"
    assert left.reconciliation_state == "matched"
    assert left.close_task_state == "completed"
    assert left.closed is True
    assert left.close_cycle_ordinal == 1
    assert left.close_cycle_seconds is not None
    assert left.close_cycle_seconds >= 0.0

    task = persistence.entity("close_task", entities.close_task_id)
    completion = next(
        event
        for event in persistence.events()
        if event.entity_type == "close_task"
        and event.entity_id == entities.close_task_id
        and event.payload.get("to_state") == "completed"
    )
    assert task is not None and task.created_at is not None
    assert left.close_cycle_seconds == max(
        0.0,
        (completion.occurred_at - task.created_at).total_seconds(),
    )

    after = persistence.entity("accounting_period", entities.period_id)
    assert after is not None and after.version == before_version


def test_r2r_projection_does_not_invent_adjustment_or_close_before_execution() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)

    projection = record_to_report_projection(persistence, entities=entities)
    kpis = record_to_report_kpis(persistence, entities=entities)

    assert projection.period_state == "open"
    assert projection.adjustment_id is None
    assert projection.adjustment_state is None
    assert projection.close_task_state == "pending"
    assert projection.closed is False
    assert projection.close_cycle_seconds is None
    assert kpis.transition_count == 0
    assert kpis.close_count == 0
    assert kpis.reopen_count == 0


def test_r2r_kpis_preserve_adjustment_history() -> None:
    persistence, entities = run_adjustment_path()

    projection = record_to_report_projection(persistence, entities=entities)
    kpis = record_to_report_kpis(persistence, entities=entities)

    assert projection.adjustment_id is not None
    assert projection.adjustment_state == "posted"
    assert projection.reconciliation_state == "reconciled"
    assert kpis.unmatched_count == 1
    assert kpis.adjustment_count == 1
    assert kpis.adjustment_posted_count == 1
    assert kpis.closed is True


def test_r2r_reopen_creates_new_observable_close_cycle_without_erasing_history() -> None:
    persistence, entities = run_happy_path()
    position = persistence.simulation_position()
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = build_runtime(persistence, now=now, tick=tick)
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)

    second_task = reopen_period(persistence, engine, entities=entities)
    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=second_task.id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)
    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
        task_id=second_task.id,
    )

    projection = record_to_report_projection(persistence, entities=entities)
    kpis = record_to_report_kpis(persistence, entities=entities)

    assert projection.period_state == "closed"
    assert projection.close_cycle_ordinal == 2
    assert projection.close_task_id == second_task.id
    assert projection.close_task_state == "completed"
    assert projection.close_cycle_seconds is not None
    assert kpis.close_count == 2
    assert kpis.reopen_count == 1
    assert kpis.close_task_completed_count == 2
    assert set(asdict(kpis)) == {
        "close_cycle_seconds",
        "amount",
        "transition_count",
        "rejected_posting_count",
        "unmatched_count",
        "adjustment_count",
        "adjustment_posted_count",
        "close_count",
        "reopen_count",
        "close_task_completed_count",
        "closed",
    }


def test_r2r_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()

    for evidence in (
        ProcessEvidence.KPIS,
        ProcessEvidence.ERD,
        ProcessEvidence.STATECHART_DOCUMENTATION,
        ProcessEvidence.PROCESS_DIAGRAM,
        ProcessEvidence.PROJECTION_CONTRACT,
        ProcessEvidence.CONFIGURATION_DOCUMENTATION,
    ):
        assert evidence in manifest.evidence
        assert manifest.evidence_sources[evidence]


def test_r2r_normative_pc5_documentation_covers_process_erd_and_config() -> None:
    specification = Path("docs/examples/record-to-report/specification.md").read_text(
        encoding="utf-8"
    )

    assert "erDiagram" in specification
    assert "flowchart TD" in specification

    for field_name in RecordToReportConfig.model_fields:
        assert f"`{field_name}`" in specification

    for field_name in definition.runtime_mutable_fields:
        assert f"`{field_name}`" in specification


def test_r2r_observability_import_does_not_require_simpy() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.modules['simpy'] = None; "
                "import sose.examples.record_to_report.observability"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

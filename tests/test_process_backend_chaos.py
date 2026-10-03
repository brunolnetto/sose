from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Iterator
from uuid import uuid4

import psycopg
import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from tests.support.behavioral_conformance import operational_snapshot
from tests.support.chaos import TransactionChaosSQLite, chaos_canonical_names
from tests.support.chaos_waits import (
    wait_for_marker_text,
    wait_for_process_exit,
    wait_for_process_exit_or_kill,
)


POSTGRES_DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
CANONICALS = chaos_canonical_names()
TICKS = 3
WORKER = Path(__file__).parent / "support" / "process_chaos_worker.py"
pytestmark = pytest.mark.slow
EXTENDED_PROCESS_CHAOS = os.environ.get("SOSE_EXTENDED_PROCESS_CHAOS") == "1"
FULL_PROCESS_CHAOS = os.environ.get("SOSE_FULL_PROCESS_CHAOS") == "1"
EXHAUSTIVE_PROCESS_CHAOS = os.environ.get("SOSE_EXHAUSTIVE_PROCESS_CHAOS") == "1"
CHAOS_PHASES = (
    ("before_commit", "after_commit")
    if os.environ.get("SOSE_FULL_CHAOS_PHASES") == "1"
    else ("before_commit",)
)
MARKER_WAIT_TIMEOUT_SECONDS = 45
WORKER_EXIT_TIMEOUT_SECONDS = 30
PROCESS_TERMINATION_TIMEOUT_SECONDS = 10
WORKER_FAILURE_GRACE_SECONDS = float(
    os.environ.get("SOSE_CHAOS_WORKER_FAILURE_GRACE_SECONDS", "5")
)


def _backend(origin):
    return SimPyBackend(origin=origin)


def _job(definition, persistence):
    return SimulationJob(
        job_id=f"{definition.name}-process-chaos",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )


def _trigger(name: str, tick: int) -> str:
    return f"{name}:process-chaos:{tick}"


def _run_control(definition, persistence):
    job = _job(definition, persistence)
    job.initialize()
    for tick in range(1, TICKS + 1):
        job.run_tick(trigger_id=_trigger(definition.name, tick))


def _recover_to_end(definition, persistence):
    job = _job(definition, persistence)
    state = job.state()
    if state is None or not state.initialized:
        job.initialize()

    while True:
        state = job.state()
        assert state is not None
        if state.next_tick >= TICKS and state.active_trigger_id is None:
            break
        if state.active_trigger_id is not None:
            job.run_tick(
                trigger_id=state.active_trigger_id,
                recover=True,
            )
        else:
            next_tick = state.next_tick + 1
            job.run_tick(
                trigger_id=_trigger(definition.name, next_tick),
            )
    return operational_snapshot(persistence)


def _sample_boundaries(transaction_count: int) -> tuple[int, ...]:
    assert transaction_count >= 1
    if EXHAUSTIVE_PROCESS_CHAOS:
        return tuple(range(1, transaction_count + 1))
    if FULL_PROCESS_CHAOS or EXTENDED_PROCESS_CHAOS:
        boundaries = {1, transaction_count}
        boundaries.add(max(1, transaction_count // 2))
        return tuple(sorted(boundaries))
    return (max(1, transaction_count // 2),)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL semantics require POSIX")
@pytest.mark.parametrize("name", CANONICALS)
def test_sigkill_process_recovers_at_sampled_transaction_boundaries(tmp_path, name):
    definition = builtin_catalog().get(name)

    control_path = tmp_path / name / "control.sqlite3"
    control_path.parent.mkdir(parents=True, exist_ok=True)
    counter = TransactionChaosSQLite(control_path)
    _run_control(definition, counter)
    transaction_count = counter.transaction_count
    counter.close()

    normalized = SQLiteIncrementalPersistence(control_path)
    expected = operational_snapshot(normalized)
    normalized.close()

    for phase in CHAOS_PHASES:
        for boundary in _sample_boundaries(transaction_count):
            case_dir = tmp_path / name / f"{phase}-{boundary}"
            case_dir.mkdir(parents=True, exist_ok=True)
            database = case_dir / "engine.sqlite3"
            marker = case_dir / "paused"

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(WORKER),
                    "--backend",
                    "sqlite",
                    "--name",
                    name,
                    "--pause-at",
                    str(boundary),
                    "--phase",
                    phase,
                    "--marker",
                    str(marker),
                    "--sqlite-path",
                    str(database),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            wait_for_marker_text(
                marker,
                process,
                timeout=MARKER_WAIT_TIMEOUT_SECONDS,
                premature_exit_label="chaos worker exited before pause marker",
                timeout_label="chaos worker did not reach pause marker",
                termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )
            process.kill()
            wait_for_process_exit(
                process,
                timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
                label="SIGKILL chaos worker",
                termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )
            assert process.returncode == -signal.SIGKILL

            reopened = SQLiteIncrementalPersistence(database)
            actual = _recover_to_end(definition, reopened)
            reopened.close()
            assert actual == expected, (
                f"{name} diverged after SIGKILL at {phase} transaction {boundary}"
            )


class CountingPostgresPersistence(PostgresPersistence):
    def __init__(self, *args, **kwargs):
        self.transaction_count = 0
        super().__init__(*args, **kwargs)

    @contextmanager
    def transaction(self, *, owner_epoch: int | None = None) -> Iterator:
        self.transaction_count += 1
        with super().transaction(owner_epoch=owner_epoch) as uow:
            yield uow


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="SOSE_TEST_POSTGRES_DSN is required for PostgreSQL backend chaos",
)
@pytest.mark.parametrize("name", CANONICALS)
def test_postgres_backend_termination_recovers_same_semantic_state(tmp_path, name):
    assert POSTGRES_DSN is not None
    definition = builtin_catalog().get(name)

    control_namespace = f"pc_control_{uuid4().hex[:16]}"
    control = CountingPostgresPersistence(
        POSTGRES_DSN,
        namespace=control_namespace,
    )
    _run_control(definition, control)
    transaction_count = control.transaction_count
    expected = operational_snapshot(control)
    control.close()

    boundary = max(1, transaction_count // 2)
    for phase in CHAOS_PHASES:
        namespace = f"pc_{uuid4().hex[:20]}"
        marker = tmp_path / f"{name}-{phase}-paused"
        continue_file = tmp_path / f"{name}-{phase}-continue"

        process = subprocess.Popen(
            [
                sys.executable,
                str(WORKER),
                "--backend",
                "postgres",
                "--name",
                name,
                "--pause-at",
                str(boundary),
                "--phase",
                phase,
                "--marker",
                str(marker),
                "--continue-file",
                str(continue_file),
                "--dsn",
                POSTGRES_DSN,
                "--namespace",
                namespace,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        pid_text = wait_for_marker_text(
            marker,
            process,
            timeout=MARKER_WAIT_TIMEOUT_SECONDS,
            premature_exit_label="chaos worker exited before pause marker",
            timeout_label="chaos worker did not reach pause marker",
            termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
        )
        backend_pid = int(pid_text)

        with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
            terminated = admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (backend_pid,),
            ).fetchone()
            assert terminated is not None and terminated[0] is True

        time.sleep(0.05)
        continue_file.write_text("continue", encoding="utf-8")
        timed_out, stdout, stderr = wait_for_process_exit_or_kill(
            process,
            timeout=WORKER_FAILURE_GRACE_SECONDS,
            termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
        )
        if timed_out:
            if os.name == "posix":
                assert process.returncode == -signal.SIGKILL, (
                    "PostgreSQL chaos worker hung after backend termination and "
                    "required forced kill\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
            else:
                assert process.returncode != 0, (
                    "PostgreSQL chaos worker hung after backend termination and "
                    "required forced kill\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
        assert process.returncode != 0

        reopened = PostgresPersistence(POSTGRES_DSN, namespace=namespace)
        actual = _recover_to_end(definition, reopened)
        reopened.close()
        assert actual == expected, (
            f"{name} diverged after PostgreSQL backend termination at "
            f"{phase} transaction {boundary}"
        )

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.persistent import PersistentJobRunner
from sose.jobs.runner import SimulationJob
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from tests.support.behavioral_conformance import operational_snapshot


RUN = os.environ.get("SOSE_RUN_FENCING_CHAOS") == "1"
DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
CANONICALS = builtin_catalog().names(kind="canonical")
WORKER = Path(__file__).parent / "support" / "fencing_chaos_worker.py"

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="SOSE_RUN_FENCING_CHAOS=1 is required",
)


def _backend(origin):
    return SimPyBackend(origin=origin)


def _job(definition, persistence):
    return SimulationJob(
        job_id=f"{definition.name}-fencing-chaos",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )


def _trigger(name: str, tick: int) -> str:
    return f"{name}:fencing:{tick}"


def _open(backend: str, *, path: Path | None = None, namespace: str | None = None):
    if backend == "sqlite":
        assert path is not None
        return SQLiteIncrementalPersistence(path)
    assert DSN is not None and namespace is not None
    return PostgresPersistence(DSN, namespace=namespace)


def _wait(marker: Path, process: subprocess.Popen, timeout: float = 15) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            payload = marker.read_text(encoding="utf-8").strip()
            if payload:
                try:
                    return int(payload)
                except ValueError:
                    pass
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"worker exited before claim pause (code={process.returncode})\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.01)
    process.kill()
    process.wait(timeout=10)
    raise TimeoutError(f"worker did not reach claim pause: {marker}")


def _worker_command(
    *,
    backend: str,
    name: str,
    owner: str,
    trigger: str,
    marker: Path,
    path: Path | None,
    namespace: str | None,
    continue_file: Path | None = None,
    recover: bool = False,
    result_file: Path | None = None,
    pause_on_claim: bool = True,
    pause_at: int | None = None,
    pause_phase: str | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(WORKER),
        "--backend",
        backend,
        "--name",
        name,
        "--owner-id",
        owner,
        "--trigger-id",
        trigger,
        "--marker",
        str(marker),
    ]
    if pause_on_claim:
        command.append("--pause-on-claim")
    if recover:
        command.append("--recover")
    if result_file is not None:
        command += ["--result-file", str(result_file)]
    if pause_at is not None:
        command += ["--pause-at", str(pause_at)]
    if pause_phase is not None:
        command += ["--pause-phase", pause_phase]
    if continue_file is not None:
        command += ["--continue-file", str(continue_file)]
    if backend == "sqlite":
        assert path is not None
        command += ["--sqlite-path", str(path)]
    else:
        assert DSN is not None and namespace is not None
        command += ["--dsn", DSN, "--namespace", namespace]
    return command


def _control(definition, backend: str, *, path: Path | None, namespace: str | None):
    persistence = _open(backend, path=path, namespace=namespace)
    runner = PersistentJobRunner(_job(definition, persistence), owner_id="control")
    for tick in range(1, 4):
        runner.run_tick(trigger_id=_trigger(definition.name, tick))
    snapshot = operational_snapshot(persistence)
    persistence.close()
    return snapshot


def _finish_after_takeover(
    definition,
    backend: str,
    *,
    path: Path | None,
    namespace: str | None,
    first_trigger: str,
    expected_previous_epoch: int,
):
    persistence = _open(backend, path=path, namespace=namespace)
    runner = PersistentJobRunner(_job(definition, persistence), owner_id="worker-b")
    recovered = runner.run_tick(trigger_id=first_trigger, recover=True)
    assert recovered.epoch == expected_previous_epoch + 1
    for tick in range(2, 4):
        runner.run_tick(trigger_id=_trigger(definition.name, tick))
    snapshot = operational_snapshot(persistence)
    persistence.close()
    return snapshot


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL requires POSIX")
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("name", CANONICALS)
def test_dead_worker_is_fenced_out_and_successor_recovers(tmp_path, backend, name):
    if backend == "postgres" and not DSN:
        pytest.skip("SOSE_TEST_POSTGRES_DSN is required")

    definition = builtin_catalog().get(name)
    control_path = tmp_path / name / backend / "control.sqlite3"
    data_path = tmp_path / name / backend / "takeover.sqlite3"
    control_namespace = f"fc_{uuid4().hex[:20]}" if backend == "postgres" else None
    namespace = f"ft_{uuid4().hex[:20]}" if backend == "postgres" else None
    expected = _control(
        definition,
        backend,
        path=control_path if backend == "sqlite" else None,
        namespace=control_namespace,
    )

    marker = tmp_path / name / backend / "worker-a-claimed"
    marker.parent.mkdir(parents=True, exist_ok=True)
    trigger = _trigger(name, 1)
    worker = subprocess.Popen(
        _worker_command(
            backend=backend,
            name=name,
            owner="worker-a",
            trigger=trigger,
            marker=marker,
            path=data_path if backend == "sqlite" else None,
            namespace=namespace,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    epoch_a = _wait(marker, worker)
    worker.kill()
    worker.wait(timeout=10)
    assert worker.returncode == -signal.SIGKILL

    actual = _finish_after_takeover(
        definition,
        backend,
        path=data_path if backend == "sqlite" else None,
        namespace=namespace,
        first_trigger=trigger,
        expected_previous_epoch=epoch_a,
    )
    assert actual == expected


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL requires POSIX")
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("name", CANONICALS)
def test_takeover_waits_for_open_fenced_transaction_then_recovers(
    tmp_path, backend, name
):
    if backend == "postgres" and not DSN:
        pytest.skip("SOSE_TEST_POSTGRES_DSN is required")

    definition = builtin_catalog().get(name)
    control_path = tmp_path / name / backend / "open-tx-control.sqlite3"
    data_path = tmp_path / name / backend / "open-tx.sqlite3"
    control_namespace = f"oc_{uuid4().hex[:20]}" if backend == "postgres" else None
    namespace = f"ot_{uuid4().hex[:20]}" if backend == "postgres" else None
    expected = _control(
        definition,
        backend,
        path=control_path if backend == "sqlite" else None,
        namespace=control_namespace,
    )

    trigger = _trigger(name, 1)
    transaction_marker = tmp_path / name / backend / "worker-a-open-transaction"
    transaction_marker.parent.mkdir(parents=True, exist_ok=True)
    worker_a = subprocess.Popen(
        _worker_command(
            backend=backend,
            name=name,
            owner="worker-a",
            trigger=trigger,
            marker=transaction_marker,
            path=data_path if backend == "sqlite" else None,
            namespace=namespace,
            pause_on_claim=False,
            pause_at=1,
            pause_phase="before_commit",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    epoch_a = _wait(transaction_marker, worker_a)

    takeover_result = tmp_path / name / backend / "worker-b-result"
    worker_b = subprocess.Popen(
        _worker_command(
            backend=backend,
            name=name,
            owner="worker-b",
            trigger=trigger,
            marker=tmp_path / name / backend / "worker-b-unused-marker",
            path=data_path if backend == "sqlite" else None,
            namespace=namespace,
            recover=True,
            result_file=takeover_result,
            pause_on_claim=False,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Worker B must not be able to advance the ownership epoch while worker A
    # still holds an open fenced transaction. SQLite serializes this with
    # BEGIN IMMEDIATE; PostgreSQL uses shared/exclusive advisory transaction
    # locks around fenced work and ownership claims.
    time.sleep(0.25)
    assert worker_b.poll() is None
    assert not takeover_result.exists()

    worker_a.kill()
    worker_a.wait(timeout=10)
    assert worker_a.returncode == -signal.SIGKILL

    stdout_b, stderr_b = worker_b.communicate(timeout=15)
    assert worker_b.returncode == 0, (
        f"takeover worker failed\nstdout={stdout_b}\nstderr={stderr_b}"
    )
    owner_b, epoch_b_text, logical_tick_text = takeover_result.read_text(
        encoding="utf-8"
    ).strip().split(":")
    assert owner_b == "worker-b"
    assert int(epoch_b_text) == epoch_a + 1
    assert int(logical_tick_text) == 1

    persistence = _open(
        backend,
        path=data_path if backend == "sqlite" else None,
        namespace=namespace,
    )
    runner = PersistentJobRunner(_job(definition, persistence), owner_id="worker-b")
    for tick in range(2, 4):
        runner.run_tick(trigger_id=_trigger(definition.name, tick))
    actual = operational_snapshot(persistence)
    persistence.close()

    assert actual == expected


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("name", CANONICALS)
def test_zombie_worker_cannot_commit_after_successor_claims_epoch(
    tmp_path, backend, name
):
    if backend == "postgres" and not DSN:
        pytest.skip("SOSE_TEST_POSTGRES_DSN is required")

    definition = builtin_catalog().get(name)
    control_path = tmp_path / name / backend / "zombie-control.sqlite3"
    data_path = tmp_path / name / backend / "zombie.sqlite3"
    control_namespace = f"zc_{uuid4().hex[:20]}" if backend == "postgres" else None
    namespace = f"zz_{uuid4().hex[:20]}" if backend == "postgres" else None
    expected = _control(
        definition,
        backend,
        path=control_path if backend == "sqlite" else None,
        namespace=control_namespace,
    )

    marker = tmp_path / name / backend / "zombie-claimed"
    continue_file = tmp_path / name / backend / "zombie-continue"
    marker.parent.mkdir(parents=True, exist_ok=True)
    trigger = _trigger(name, 1)
    worker = subprocess.Popen(
        _worker_command(
            backend=backend,
            name=name,
            owner="worker-a",
            trigger=trigger,
            marker=marker,
            path=data_path if backend == "sqlite" else None,
            namespace=namespace,
            continue_file=continue_file,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    epoch_a = _wait(marker, worker)

    actual = _finish_after_takeover(
        definition,
        backend,
        path=data_path if backend == "sqlite" else None,
        namespace=namespace,
        first_trigger=trigger,
        expected_previous_epoch=epoch_a,
    )

    continue_file.write_text("continue", encoding="utf-8")
    worker.wait(timeout=10)
    stdout, stderr = worker.communicate()
    assert worker.returncode != 0
    assert "StaleWriterError" in stderr

    final_persistence = _open(
        backend,
        path=data_path if backend == "sqlite" else None,
        namespace=namespace,
    )
    final_snapshot = operational_snapshot(final_persistence)
    final_persistence.close()

    assert actual == expected
    assert final_snapshot == expected

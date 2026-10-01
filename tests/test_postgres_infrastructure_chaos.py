from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Iterator
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.postgres import PostgresPersistence
from tests.support.behavioral_conformance import operational_snapshot


RUN = os.environ.get("SOSE_RUN_POSTGRES_INFRA_CHAOS") == "1"
DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
CONTAINER_ID = os.environ.get("SOSE_POSTGRES_CONTAINER_ID")
CANONICALS = builtin_catalog().names(kind="canonical")
WORKER = Path(__file__).parent / "support" / "process_chaos_worker.py"
PROXY = Path(__file__).parent / "support" / "tcp_chaos_proxy.py"

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="SOSE_RUN_POSTGRES_INFRA_CHAOS=1 is required",
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


def _control(definition, namespace: str):
    assert DSN is not None
    persistence = CountingPostgresPersistence(DSN, namespace=namespace)
    job = _job(definition, persistence)
    job.initialize()
    for tick in range(1, 4):
        job.run_tick(trigger_id=_trigger(definition.name, tick))
    snapshot = operational_snapshot(persistence)
    count = persistence.transaction_count
    persistence.close()
    return snapshot, count


def _recover(definition, dsn: str, namespace: str):
    persistence = PostgresPersistence(dsn, namespace=namespace)
    job = _job(definition, persistence)
    state = job.state()
    if state is None or not state.initialized:
        job.initialize()

    while True:
        state = job.state()
        assert state is not None
        if state.next_tick >= 3 and state.active_trigger_id is None:
            break
        if state.active_trigger_id is not None:
            job.run_tick(
                trigger_id=state.active_trigger_id,
                recover=True,
            )
        else:
            tick = state.next_tick + 1
            job.run_tick(trigger_id=_trigger(definition.name, tick))

    snapshot = operational_snapshot(persistence)
    persistence.close()
    return snapshot


def _wait_marker(marker: Path, process: subprocess.Popen, timeout: float = 20) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"worker exited before chaos pause (code={process.returncode})\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.01)
    process.kill()
    process.wait(timeout=10)
    raise TimeoutError(f"worker did not reach chaos pause: {marker}")


def _wait_file(marker: Path, process: subprocess.Popen, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"proxy exited before ready (code={process.returncode})\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.01)
    process.kill()
    process.wait(timeout=10)
    raise TimeoutError(f"proxy did not become ready: {marker}")


def _paused_worker(
    *,
    name: str,
    dsn: str,
    namespace: str,
    boundary: int,
    phase: str,
    marker: Path,
    continue_file: Path,
) -> subprocess.Popen:
    return subprocess.Popen(
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
            dsn,
            "--namespace",
            namespace,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_postgres(dsn: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
                return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"PostgreSQL did not recover: {last_error}")


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_proxy(dsn: str, *, port: int, marker: Path) -> tuple[subprocess.Popen, str]:
    info = conninfo_to_dict(dsn)
    host = info.get("host", "127.0.0.1")
    upstream_port = int(info.get("port", "5432"))
    marker.unlink(missing_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROXY),
            "--listen-port",
            str(port),
            "--upstream-host",
            host,
            "--upstream-port",
            str(upstream_port),
            "--marker",
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_file(marker, process)
    proxied = make_conninfo(
        dsn,
        host="127.0.0.1",
        port=str(port),
        connect_timeout="3",
    )
    return process, proxied


@pytest.mark.parametrize("name", CANONICALS)
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_whole_postgres_outage_and_restart_recovers_control_state(
    tmp_path, name, phase
):
    if not DSN or not CONTAINER_ID:
        pytest.skip("PostgreSQL DSN and service container id are required")

    definition = builtin_catalog().get(name)
    expected, transaction_count = _control(
        definition,
        f"outage_control_{uuid4().hex[:16]}",
    )
    boundary = max(1, transaction_count // 2)
    namespace = f"outage_{uuid4().hex[:20]}"
    marker = tmp_path / f"{name}-{phase}-outage-paused"
    continue_file = tmp_path / f"{name}-{phase}-outage-continue"

    worker = _paused_worker(
        name=name,
        dsn=DSN,
        namespace=namespace,
        boundary=boundary,
        phase=phase,
        marker=marker,
        continue_file=continue_file,
    )
    _wait_marker(marker, worker)

    _docker("stop", "-t", "1", CONTAINER_ID)
    try:
        continue_file.write_text("continue", encoding="utf-8")
        worker.wait(timeout=15)
        assert worker.returncode != 0
    finally:
        _docker("start", CONTAINER_ID)
        _wait_postgres(DSN)

    actual = _recover(definition, DSN, namespace)
    assert actual == expected, (
        f"{name} diverged after whole PostgreSQL outage at {phase} boundary"
    )


@pytest.mark.parametrize("name", CANONICALS)
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_network_interruption_through_tcp_proxy_recovers_control_state(
    tmp_path, name, phase
):
    if not DSN:
        pytest.skip("SOSE_TEST_POSTGRES_DSN is required")

    definition = builtin_catalog().get(name)
    expected, transaction_count = _control(
        definition,
        f"network_control_{uuid4().hex[:16]}",
    )
    boundary = max(1, transaction_count // 2)
    namespace = f"network_{uuid4().hex[:20]}"
    port = _free_port()
    proxy_marker = tmp_path / f"{name}-{phase}-proxy-ready"
    proxy, proxied_dsn = _start_proxy(DSN, port=port, marker=proxy_marker)

    worker_marker = tmp_path / f"{name}-{phase}-network-paused"
    continue_file = tmp_path / f"{name}-{phase}-network-continue"
    worker = _paused_worker(
        name=name,
        dsn=proxied_dsn,
        namespace=namespace,
        boundary=boundary,
        phase=phase,
        marker=worker_marker,
        continue_file=continue_file,
    )
    _wait_marker(worker_marker, worker)

    proxy.kill()
    proxy.wait(timeout=10)
    continue_file.write_text("continue", encoding="utf-8")
    worker.wait(timeout=15)
    assert worker.returncode != 0

    recovered_proxy_marker = tmp_path / f"{name}-{phase}-proxy-recovered"
    recovered_proxy, recovered_dsn = _start_proxy(
        DSN,
        port=port,
        marker=recovered_proxy_marker,
    )
    try:
        actual = _recover(definition, recovered_dsn, namespace)
    finally:
        recovered_proxy.kill()
        recovered_proxy.wait(timeout=10)

    assert actual == expected, (
        f"{name} diverged after network interruption at {phase} boundary"
    )

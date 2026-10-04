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

import psycopg  # type: ignore[import-not-found]
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # type: ignore[import-not-found]
import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.postgres import PostgresPersistence
from tests.support.behavioral_conformance import operational_snapshot
from tests.support.chaos import chaos_canonical_names
from tests.support.chaos_waits import (
    wait_for_marker_exists,
    wait_for_marker_text,
    wait_for_process_exit,
    wait_for_process_exit_or_kill,
)
from tests.support.paths import repo_root_from


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
CONTAINER_ID = os.environ.get("SOSE_POSTGRES_CONTAINER_ID")
CANONICALS = chaos_canonical_names()
CHAOS_PHASES = (
    ("before_commit", "after_commit")
    if os.environ.get("SOSE_FULL_CHAOS_PHASES") == "1"
    else ("before_commit",)
)
REPO_ROOT = repo_root_from(__file__)
WORKER = REPO_ROOT / "tests" / "support" / "process_chaos_worker.py"
PROXY = REPO_ROOT / "tests" / "support" / "tcp_chaos_proxy.py"
pytestmark = pytest.mark.slow
MARKER_WAIT_TIMEOUT_SECONDS = 45
PROXY_READY_TIMEOUT_SECONDS = 30
WORKER_EXIT_TIMEOUT_SECONDS = 30
PROCESS_TERMINATION_TIMEOUT_SECONDS = 10
WORKER_FAILURE_GRACE_SECONDS = float(
    os.environ.get("SOSE_CHAOS_WORKER_FAILURE_GRACE_SECONDS", "5")
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


@pytest.fixture(scope="module")
def _control_cache():
    return {}


def _cached_control_snapshot(definition, name: str, control_cache):
    cached = control_cache.get(name)
    if cached is not None:
        return cached
    baseline = _control(
        definition,
        f"infra_control_{uuid4().hex[:16]}",
    )
    control_cache[name] = baseline
    return baseline


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
    host_value = str(info.get("host") or "")
    host = host_value.split(",", 1)[0].strip()
    upstream_port = int(str(info.get("port") or "5432").split(",", 1)[0])
    marker.unlink(missing_ok=True)
    command = [
        sys.executable,
        str(PROXY),
        "--listen-port",
        str(port),
        "--upstream-port",
        str(upstream_port),
        "--marker",
        str(marker),
    ]
    if host.startswith("/") or host == "":
        command += ["--upstream-unix-socket-dir", host or "/var/run/postgresql"]
    else:
        command += ["--upstream-host", host]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for_marker_exists(
        marker,
        process,
        timeout=PROXY_READY_TIMEOUT_SECONDS,
        premature_exit_label="proxy exited before ready",
        timeout_label="proxy did not become ready",
        termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
    )
    proxied = make_conninfo(
        dsn,
        host="127.0.0.1",
        port=str(port),
        connect_timeout="3",
    )
    return process, proxied


@pytest.mark.parametrize("name", CANONICALS)
@pytest.mark.parametrize("phase", CHAOS_PHASES)
def test_whole_postgres_outage_and_restart_recovers_control_state(
    tmp_path,
    name,
    phase,
    _control_cache,
):
    if not DSN:
        pytest.skip("SOSE_TEST_POSTGRES_DSN is required")

    definition = builtin_catalog().get(name)
    expected, transaction_count = _cached_control_snapshot(
        definition,
        name,
        _control_cache,
    )
    boundary = max(1, transaction_count // 2)
    namespace = f"outage_{uuid4().hex[:20]}"
    marker = tmp_path / f"{name}-{phase}-outage-paused"
    continue_file = tmp_path / f"{name}-{phase}-outage-continue"

    if CONTAINER_ID:
        worker = _paused_worker(
            name=name,
            dsn=DSN,
            namespace=namespace,
            boundary=boundary,
            phase=phase,
            marker=marker,
            continue_file=continue_file,
        )
        wait_for_marker_text(
            marker,
            worker,
            timeout=MARKER_WAIT_TIMEOUT_SECONDS,
            premature_exit_label="worker exited before chaos pause",
            timeout_label="worker did not reach chaos pause",
            termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
        )

        _docker("stop", "-t", "1", CONTAINER_ID)
        try:
            continue_file.write_text("continue", encoding="utf-8")
            timed_out, stdout, stderr = wait_for_process_exit_or_kill(
                worker,
                timeout=WORKER_FAILURE_GRACE_SECONDS,
                termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )
            if timed_out:
                assert worker.returncode != 0, (
                    "outage worker required forced termination after restart "
                    "window\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
            assert worker.returncode != 0
        finally:
            _docker("start", CONTAINER_ID)
            _wait_postgres(DSN)

        actual = _recover(definition, DSN, namespace)
    else:
        port = _free_port()
        proxy_marker = tmp_path / f"{name}-{phase}-outage-proxy-ready"
        proxy, proxied_dsn = _start_proxy(DSN, port=port, marker=proxy_marker)
        try:
            worker = _paused_worker(
                name=name,
                dsn=proxied_dsn,
                namespace=namespace,
                boundary=boundary,
                phase=phase,
                marker=marker,
                continue_file=continue_file,
            )
            wait_for_marker_text(
                marker,
                worker,
                timeout=MARKER_WAIT_TIMEOUT_SECONDS,
                premature_exit_label="worker exited before chaos pause",
                timeout_label="worker did not reach chaos pause",
                termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )
            proxy.kill()
            wait_for_process_exit(
                proxy,
                timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
                label="outage proxy",
                termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )
            continue_file.write_text("continue", encoding="utf-8")
            timed_out, stdout, stderr = wait_for_process_exit_or_kill(
                worker,
                timeout=WORKER_FAILURE_GRACE_SECONDS,
                termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            )
            if timed_out:
                assert worker.returncode != 0, (
                    "outage worker required forced termination after network "
                    "outage\n"
                    f"stdout={stdout}\nstderr={stderr}"
                )
            assert worker.returncode != 0
            recovered_marker = tmp_path / f"{name}-{phase}-outage-proxy-recovered"
            recovered_proxy, recovered_dsn = _start_proxy(
                DSN,
                port=port,
                marker=recovered_marker,
            )
            try:
                actual = _recover(definition, recovered_dsn, namespace)
            finally:
                recovered_proxy.kill()
                wait_for_process_exit(
                    recovered_proxy,
                    timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
                    label="recovered outage proxy",
                    termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
                )
        finally:
            if proxy.poll() is None:
                proxy.kill()
                wait_for_process_exit(
                    proxy,
                    timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
                    label="outage proxy",
                    termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
                )

    assert actual == expected, (
        f"{name} diverged after whole PostgreSQL outage at {phase} boundary"
    )


@pytest.mark.parametrize("name", CANONICALS)
@pytest.mark.parametrize("phase", CHAOS_PHASES)
def test_network_interruption_through_tcp_proxy_recovers_control_state(
    tmp_path,
    name,
    phase,
    _control_cache,
):
    if not DSN:
        pytest.skip("SOSE_TEST_POSTGRES_DSN is required")

    definition = builtin_catalog().get(name)
    expected, transaction_count = _cached_control_snapshot(
        definition,
        name,
        _control_cache,
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
    wait_for_marker_text(
        worker_marker,
        worker,
        timeout=MARKER_WAIT_TIMEOUT_SECONDS,
        premature_exit_label="worker exited before chaos pause",
        timeout_label="worker did not reach chaos pause",
        termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
    )

    proxy.kill()
    wait_for_process_exit(
        proxy,
        timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
        label="network proxy",
        termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
    )
    continue_file.write_text("continue", encoding="utf-8")
    timed_out, stdout, stderr = wait_for_process_exit_or_kill(
        worker,
        timeout=WORKER_FAILURE_GRACE_SECONDS,
        termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
    )
    if timed_out:
        assert worker.returncode != 0, (
            "network worker required forced termination after proxy cut\n"
            f"stdout={stdout}\nstderr={stderr}"
        )
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
        wait_for_process_exit(
            recovered_proxy,
            timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
            label="recovered network proxy",
            termination_timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS,
        )

    assert actual == expected, (
        f"{name} diverged after network interruption at {phase} boundary"
    )

"""Dedicated real PostgreSQL per destructive postmaster test (#598).

A shared PostgreSQL instance must never be stopped by real fault injection.
Each destructive test receives its own Docker container and fails closed if
provisioning, recovery verification or teardown is unsuccessful. Docker may
reassign a dynamically published host port on container restart, so the
endpoint is resolved again before post-restart SQL work.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import time
from uuid import uuid4

import psycopg
import pytest


def _docker(*arguments: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", *arguments),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _published_host_port(name: str) -> int:
    """Return Docker's assigned loopback port, failing closed on bad mappings."""
    result = _docker("port", name, "5432/tcp", timeout=15)
    if result.returncode != 0:
        pytest.fail(
            f"cannot inspect isolated PostgreSQL host port ({name}): "
            + result.stderr[-1500:]
        )

    published = result.stdout.strip()
    host, separator, raw_port = published.rpartition(":")
    if not separator or host != "127.0.0.1":
        pytest.fail(
            "isolated PostgreSQL must publish exactly one loopback endpoint; "
            f"got {published!r}"
        )
    try:
        port = int(raw_port)
    except ValueError:
        pytest.fail(f"invalid isolated PostgreSQL host port: {published!r}")
    if not 1 <= port <= 65535:
        pytest.fail(f"invalid isolated PostgreSQL host port: {published!r}")
    return port


def _capture_postgres_evidence(name: str, directory: Path) -> None:
    """Capture a bounded forensic record before removing the dedicated DB."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, args in (
        ("docker-logs.txt", ("logs", "--timestamps", "--tail", "200", name)),
        ("docker-inspect.json", ("inspect", name)),
        ("docker-port.txt", ("port", name, "5432/tcp")),
    ):
        try:
            result = _docker(*args, timeout=15)
            (directory / filename).write_text(
                result.stdout + result.stderr, encoding="utf-8",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            (directory / filename).write_text(
                f"Failed to obtain Docker diagnostic: {exc}", encoding="utf-8",
            )


def _remove_isolated_postgres(name: str, directory: Path, *, provisioned: bool) -> None:
    """Always attempt named-container removal, including after inspect failure.

    Inspection is best-effort discovery, never a prerequisite for cleanup. A
    successful docker rm -f is sufficient for a previously unknown container;
    any failed removal fails the test rather than leaking trust-auth PostgreSQL.
    """
    inspect_error = None
    try:
        _capture_postgres_evidence(name, directory)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Diagnostic errors must never prevent the teardown attempt.
        inspect_error = f"diagnostic capture failed: {exc}"

    try:
        inspected = _docker("inspect", name, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        inspect_error = f"container inspection failed: {exc}"
    else:
        if inspected.returncode != 0:
            if "No such object" in inspected.stderr and not provisioned:
                return
            inspect_error = (
                "container inspection returned nonzero: " + inspected.stderr[-1500:]
            )

    # Crucial: even when inspect timed out or failed, use the known unique
    # container name to remove any object docker run might have left behind.
    try:
        removed = _docker("rm", "-f", name, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(
            f"cannot remove isolated PostgreSQL {name}: {exc}; "
            f"inspection: {inspect_error}"
        )
    if removed.returncode != 0:
        pytest.fail(
            f"isolated PostgreSQL container removal failed ({name}): "
            + removed.stderr[-1500:]
            + (f"; inspection: {inspect_error}" if inspect_error else "")
        )


class _IsolatedPostgresService:
    """Dedicated PostgreSQL container whose dynamic endpoint may change on restart."""

    def __init__(self, *, name, container_id, port, module, monkeypatch):
        self.name = name
        self.container_id = container_id
        self.port = port
        self.dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        self.module = module
        self.monkeypatch = monkeypatch

    def _set_endpoint(self, port: int) -> None:
        self.port = port
        self.dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
        self.monkeypatch.setattr(self.module, "DSN", self.dsn)
        self.monkeypatch.setenv("SOSE_TEST_POSTGRES_DSN", self.dsn)

    def restart(self) -> str:
        """Restart the same container, resolve its current host port, and await SQL."""
        try:
            started = _docker("start", self.container_id, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            pytest.fail(f"isolated PostgreSQL restart command failed: {exc}")
        if started.returncode != 0:
            pytest.fail(
                "isolated PostgreSQL container restart failed: "
                + started.stderr[-1500:]
            )

        deadline = time.monotonic() + 120.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            port = _published_host_port(self.name)
            dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
            try:
                with psycopg.connect(dsn, connect_timeout=2) as connection:
                    assert connection.execute("SELECT 1").fetchone() == (1,)
            except (psycopg.Error, AssertionError) as error:
                last_error = error
                time.sleep(0.25)
                continue
            self._set_endpoint(port)
            return self.dsn

        pytest.fail(
            "isolated PostgreSQL did not become SQL-ready after restart: "
            f"{last_error}"
        )


@pytest.fixture
def isolated_postgres_service(request, monkeypatch, tmp_path):
    """Physically destructive variants only; proxy cases keep the shared DB.

    Docker allocates the loopback host port atomically, avoiding the race in
    bind(127.0.0.1, 0) -> close -> docker run -p <port>. Because Docker may
    reassign that dynamic port on restart, consumers must call service.restart()
    before post-restart SQL work; it refreshes the module DSN after SQL readiness.
    """
    parameters = getattr(getattr(request.node, "callspec", None), "params", {})
    if parameters.get("transport") == "proxy":
        yield
        return

    name = f"sose-pg-chaos-{uuid4().hex[:16]}"
    diagnostics = tmp_path / "isolated-postgres"
    provisioned = False

    # This try/finally includes docker run itself. Even a timeout or lost
    # container-ID reply must trigger discovery and cleanup by known name.
    try:
        try:
            run = _docker(
                "run", "-d", "--name", name,
                "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e", "POSTGRES_DB=postgres",
                "-p", "127.0.0.1::5432",
                "postgres:16-alpine", timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            pytest.fail(f"isolated PostgreSQL provisioning command failed: {exc}")

        if run.returncode != 0:
            pytest.fail(
                "real isolated PostgreSQL container provisioning failed: "
                + run.stderr[-1500:]
            )
        container_id = run.stdout.strip()
        if not container_id:
            pytest.fail("docker run returned no PostgreSQL container identifier")
        provisioned = True

        port = _published_host_port(name)
        dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"

        deadline = time.monotonic() + 120.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as connection:
                    assert connection.execute("SELECT 1").fetchone() == (1,)
                break
            except (psycopg.Error, AssertionError) as error:
                last_error = error
                time.sleep(0.25)
        else:
            pytest.fail(f"isolated PostgreSQL never became ready: {last_error}")

        if _published_host_port(name) != port:
            pytest.fail(
                "isolated PostgreSQL host port changed during readiness check"
            )

        module = request.module
        service = _IsolatedPostgresService(
            name=name,
            container_id=container_id,
            port=port,
            module=module,
            monkeypatch=monkeypatch,
        )
        monkeypatch.setattr(module, "DSN", dsn)
        monkeypatch.setattr(module, "CONTAINER_ID", container_id)
        monkeypatch.setenv("SOSE_TEST_POSTGRES_DSN", dsn)
        monkeypatch.setenv("SOSE_POSTGRES_CONTAINER_ID", container_id)
        yield service

        # Require the endpoint to match the latest Docker mapping. It may differ
        # from the initial port, but a successful restart must refresh the DSN.
        after = _published_host_port(name)
        if after != service.port:
            pytest.fail(
                f"PostgreSQL host port changed without endpoint refresh: "
                f"{after!r}; expected {service.port}"
            )
        with psycopg.connect(service.dsn, connect_timeout=3) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
    finally:
        _remove_isolated_postgres(name, diagnostics, provisioned=provisioned)

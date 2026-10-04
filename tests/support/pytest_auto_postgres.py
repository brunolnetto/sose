from __future__ import annotations

import os
import socket
import subprocess
import time


_CANDIDATE_DSNS = [
    "postgresql://postgres@127.0.0.1/postgres",
    "postgresql://postgres@localhost/postgres",
    "postgresql:///pingu",
    "postgresql:///postgres",
]
_AUTOSTARTED_CONTAINER_ID: str | None = None


def _probe_dsn(dsn: str) -> bool:
    try:
        import psycopg  # type: ignore[import-not-found]

        conn = psycopg.connect(dsn, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


def _discover_postgres_container_id(dsn: str) -> str | None:
    try:
        from psycopg.conninfo import conninfo_to_dict  # type: ignore[import-not-found]

        info = conninfo_to_dict(dsn)
        host = str(info.get("host") or "localhost")
        port = str(info.get("port") or "5432")
        if host not in {"localhost", "127.0.0.1"}:
            return None
        probe = subprocess.run(
            ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return None
        candidates = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if len(candidates) == 1:
            return candidates[0]
        return None
    except Exception:
        return None


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_postgres_ready(dsn: str, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _probe_dsn(dsn):
            return True
        time.sleep(0.25)
    return False


def _start_ephemeral_postgres_container() -> tuple[str, str] | None:
    port = _free_local_port()
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_DB=postgres",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:16-alpine",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        return None
    container_id = run.stdout.strip()
    if not container_id:
        return None
    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    if not _wait_postgres_ready(dsn):
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            check=False,
            capture_output=True,
            text=True,
        )
        return None
    return container_id, dsn


def pytest_configure(config) -> None:
    """Auto-detect a local PostgreSQL instance when no DSN is supplied.

    Sets SOSE_TEST_POSTGRES_DSN so that postgres-gated tests run without
    requiring the caller to export the variable manually.
    """
    global _AUTOSTARTED_CONTAINER_ID
    explicit_dsn = bool(os.environ.get("SOSE_TEST_POSTGRES_DSN"))

    dsn = os.environ.get("SOSE_TEST_POSTGRES_DSN")
    if not dsn:
        for candidate in _CANDIDATE_DSNS:
            if _probe_dsn(candidate):
                dsn = candidate
                os.environ["SOSE_TEST_POSTGRES_DSN"] = candidate
                break

    if not dsn:
        provisioned = _start_ephemeral_postgres_container()
        if provisioned is not None:
            _AUTOSTARTED_CONTAINER_ID, dsn = provisioned
            os.environ["SOSE_TEST_POSTGRES_DSN"] = dsn
            os.environ["SOSE_POSTGRES_CONTAINER_ID"] = _AUTOSTARTED_CONTAINER_ID

    if dsn and not os.environ.get("SOSE_POSTGRES_CONTAINER_ID"):
        container_id = _discover_postgres_container_id(dsn)
        if container_id:
            os.environ["SOSE_POSTGRES_CONTAINER_ID"] = container_id
        elif not explicit_dsn:
            provisioned = _start_ephemeral_postgres_container()
            if provisioned is not None:
                _AUTOSTARTED_CONTAINER_ID, dsn = provisioned
                os.environ["SOSE_TEST_POSTGRES_DSN"] = dsn
                os.environ["SOSE_POSTGRES_CONTAINER_ID"] = _AUTOSTARTED_CONTAINER_ID


def pytest_unconfigure(config) -> None:
    if not _AUTOSTARTED_CONTAINER_ID:
        return
    subprocess.run(
        ["docker", "rm", "-f", _AUTOSTARTED_CONTAINER_ID],
        check=False,
        capture_output=True,
        text=True,
    )

"""#598: destructive PostgreSQL fixture must fail closed without leaking Docker.

Unit-level fault injection tests intentionally do NOT launch Docker or a live
database. The separate e2e tests certify genuine postmaster stop/start.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
from types import SimpleNamespace

import pytest

from tests.e2e.sose.persistence import conftest as pg_chaos


def _result(*args, code=0, out="", err=""):
    return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


def test_published_host_port_accepts_docker_assigned_loopback_mapping(monkeypatch):
    monkeypatch.setattr(
        pg_chaos, "_docker",
        lambda *args, timeout=30: _result(*args, out="127.0.0.1:55439\n"),
    )
    assert pg_chaos._published_host_port("sose-pg-chaos-test") == 55439


@pytest.mark.parametrize(
    "output",
    (
        "0.0.0.0:55439",
        "127.0.0.1:not-a-port",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:55439\n127.0.0.1:55440",
    ),
)
def test_published_host_port_rejects_invalid_or_ambiguous_mapping(monkeypatch, output):
    monkeypatch.setattr(
        pg_chaos, "_docker",
        lambda *args, timeout=30: _result(*args, out=output),
    )
    with pytest.raises(pytest.fail.Exception):
        pg_chaos._published_host_port("sose-pg-chaos-test")


def test_published_host_port_fails_when_docker_port_command_fails(monkeypatch):
    monkeypatch.setattr(
        pg_chaos, "_docker",
        lambda *args, timeout=30: _result(*args, code=1, err="daemon unavailable"),
    )
    with pytest.raises(pytest.fail.Exception, match="daemon unavailable"):
        pg_chaos._published_host_port("sose-pg-chaos-test")


@pytest.mark.parametrize("fault", ("run_timeout", "empty_id", "run_error"))
def test_postgres_provisioning_failures_still_clean_up_named_container(
    monkeypatch, tmp_path: Path, fault: str,
):
    seen: list[tuple[str, ...]] = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "run":
            if fault == "run_timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            if fault == "empty_id":
                return _result(*args)
            return _result(*args, code=125, err="Docker create failed")
        if args[0] == "inspect":
            # A container exists under its assigned name despite loss of
            # docker run's success response. It MUST be removed.
            return _result(*args, out="{}")
        if args[:2] == ("rm", "-f"):
            return _result(*args, out="removed")
        return _result(*args, out="diagnostic")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    request = SimpleNamespace(
        node=SimpleNamespace(callspec=SimpleNamespace(params={"transport": "server"})),
    )
    generator = pg_chaos.isolated_postgres_service.__wrapped__(
        request, monkeypatch, tmp_path,
    )
    with pytest.raises(pytest.fail.Exception):
        next(generator)

    creations = [args for args in seen if args[0] == "run"]
    removals = [args for args in seen if args[:2] == ("rm", "-f")]
    assert len(creations) == 1
    assert len(removals) == 1
    assert creations[0][creations[0].index("--name") + 1] == removals[0][2]
    assert creations[0][creations[0].index("-p") + 1] == "127.0.0.1::5432"
    assert (tmp_path / "isolated-postgres/docker-inspect.json").is_file()


def test_postgres_cleanup_refuses_silent_docker_rm_failure(monkeypatch, tmp_path: Path):
    def fake_docker(*args: str, timeout: int = 30):
        if args[0] == "inspect":
            return _result(*args, out="{}")
        if args[:2] == ("rm", "-f"):
            return _result(*args, code=1, err="permission denied")
        return _result(*args, out="diagnostic")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    with pytest.raises(pytest.fail.Exception, match="container removal failed.*permission denied"):
        pg_chaos._remove_isolated_postgres(
            "sose-pg-chaos-test", tmp_path, provisioned=True,
        )
    assert (tmp_path / "docker-logs.txt").is_file()


def test_failed_provisioning_without_created_container_preserves_original_error(
    monkeypatch, tmp_path: Path,
):
    seen = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "inspect":
            return _result(*args, code=1, err="Error: No such object: missing")
        return _result(*args, code=1, err="not found")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    pg_chaos._remove_isolated_postgres("missing", tmp_path, provisioned=False)
    assert not any(args[:2] == ("rm", "-f") for args in seen)


def test_isolated_fixture_does_not_launch_docker_for_proxy_cases(monkeypatch, tmp_path):
    def forbidden_docker(*args, **kwargs):
        raise AssertionError("proxy test must not create or stop a postmaster")

    monkeypatch.setattr(pg_chaos, "_docker", forbidden_docker)
    request = SimpleNamespace(
        node=SimpleNamespace(callspec=SimpleNamespace(params={"transport": "proxy"})),
    )
    fixture = pg_chaos.isolated_postgres_service.__wrapped__(
        request, monkeypatch, tmp_path,
    )
    assert next(fixture) is None
    with pytest.raises(StopIteration):
        next(fixture)


@pytest.mark.parametrize("inspect_failure", ("timeout", "error"))
def test_cleanup_always_attempts_removal_when_docker_inspect_fails(
    monkeypatch, tmp_path: Path, inspect_failure: str,
):
    seen = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "inspect":
            if inspect_failure == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            return _result(*args, code=1, err="Docker engine disconnected")
        if args[:2] == ("rm", "-f"):
            return _result(*args, out="removed")
        return _result(*args, out="diagnostic")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    pg_chaos._remove_isolated_postgres("sose-pg-chaos-dangling", tmp_path, provisioned=True)
    assert ("rm", "-f", "sose-pg-chaos-dangling") in seen
    assert (tmp_path / "docker-logs.txt").exists()


def test_inspect_timeout_and_failed_removal_must_fail_closed(monkeypatch, tmp_path: Path):
    seen = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "inspect":
            raise subprocess.TimeoutExpired(args, timeout)
        if args[:2] == ("rm", "-f"):
            return _result(*args, code=1, err="cannot contact Docker daemon")
        return _result(*args, out="diagnostic")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    with pytest.raises(pytest.fail.Exception, match="container removal failed"):
        pg_chaos._remove_isolated_postgres(
            "sose-pg-chaos-dangling", tmp_path, provisioned=False,
        )
    assert ("rm", "-f", "sose-pg-chaos-dangling") in seen


def test_service_restart_refreshes_docker_reassigned_host_port(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            assert statement == "SELECT 1"
            return self

        def fetchone(self):
            return (1,)

    seen = []

    def fake_docker(*args, timeout=30):
        seen.append(args)
        if args[0] == "start":
            return _result(*args, out="container-id")
        if args[0] == "port":
            return _result(*args, out="127.0.0.1:60083\n")
        raise AssertionError(f"unexpected Docker command: {args}")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    monkeypatch.setattr(pg_chaos.psycopg, "connect", lambda *args, **kwargs: Connection())
    module = SimpleNamespace(DSN="postgresql://postgres@127.0.0.1:52431/postgres")
    service = pg_chaos._IsolatedPostgresService(
        name="sose-pg-chaos-test",
        container_id="container-id",
        port=52431,
        module=module,
        monkeypatch=monkeypatch,
    )

    dsn = service.restart()

    assert dsn == "postgresql://postgres@127.0.0.1:60083/postgres"
    assert service.port == 60083
    assert module.DSN == dsn
    assert os.environ["SOSE_TEST_POSTGRES_DSN"] == dsn
    assert ("start", "container-id") in seen

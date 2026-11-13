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


@pytest.fixture(autouse=True)
def _no_shared_postgres(monkeypatch):
    """Keep these unit tests hermetic even when a shared test DSN is exported."""
    monkeypatch.delenv("SOSE_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("SOSE_POSTGRES_CONTAINER_ID", raising=False)


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        assert statement == "SELECT 1"
        return self

    def fetchone(self):
        return (1,)


def _healthy_docker(seen):
    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "run":
            return _result(*args, out="container-id\n")
        if args[0] == "port":
            return _result(*args, out="127.0.0.1:55501\n")
        if args[:2] == ("rm", "-f"):
            return _result(*args, out="removed")
        return _result(*args, out="{}")
    return fake_docker


def _server_request(module):
    return SimpleNamespace(
        node=SimpleNamespace(callspec=SimpleNamespace(params={"transport": "server"})),
        module=module,
    )


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
        if args[:2] == ("rm", "-f"):
            return _result(*args, code=1, err="Error response from daemon: No such container: missing")
        return _result(*args, code=1, err="not found")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    # Removal is still attempted, but a confirmed-absent container must not
    # replace the original provisioning failure with a teardown failure.
    pg_chaos._remove_isolated_postgres("missing", tmp_path, provisioned=False)
    assert ("rm", "-f", "missing") in seen


def test_cleanup_removes_container_that_materializes_after_absent_inspect(
    monkeypatch, tmp_path: Path,
):
    """A lost docker run reply may surface the container only after inspect."""
    seen = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "inspect":
            return _result(*args, code=1, err="Error: No such object: late")
        if args[:2] == ("rm", "-f"):
            return _result(*args, out="late")
        return _result(*args, out="diagnostic")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    pg_chaos._remove_isolated_postgres("late", tmp_path, provisioned=False)
    assert ("rm", "-f", "late") in seen


def test_absent_inspect_does_not_excuse_other_removal_failures(monkeypatch, tmp_path: Path):
    def fake_docker(*args: str, timeout: int = 30):
        if args[0] == "inspect":
            return _result(*args, code=1, err="Error: No such object: busy")
        if args[:2] == ("rm", "-f"):
            return _result(*args, code=1, err="removal of container busy is already in progress")
        return _result(*args, out="diagnostic")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    with pytest.raises(pytest.fail.Exception, match="already in progress"):
        pg_chaos._remove_isolated_postgres("busy", tmp_path, provisioned=False)


def test_isolated_postgres_uses_ci_major_version_by_default():
    assert pg_chaos.ISOLATED_POSTGRES_IMAGE == "postgres:18"


def test_missing_image_is_pulled_outside_container_start_budget(monkeypatch):
    seen = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append((args, timeout))
        if args[:2] == ("image", "inspect"):
            return _result(*args, code=1, err="No such image")
        return _result(*args, out="pulled")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    pg_chaos._ensure_image("postgres:18")
    assert (("pull", "postgres:18"), 600) in seen


def test_failed_image_pull_fails_closed(monkeypatch):
    def fake_docker(*args: str, timeout: int = 30):
        if args[:2] == ("image", "inspect"):
            return _result(*args, code=1, err="No such image")
        return _result(*args, code=1, err="toomanyrequests: rate limit")

    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    with pytest.raises(pytest.fail.Exception, match="rate limit"):
        pg_chaos._ensure_image("postgres:18")


def test_readiness_failure_reports_docker_state_and_log_tail(monkeypatch, tmp_path: Path):
    seen = []

    def fake_docker(*args: str, timeout: int = 30):
        seen.append(args)
        if args[0] == "run":
            return _result(*args, out="container-id\n")
        if args[0] == "port":
            return _result(*args, out="127.0.0.1:55501\n")
        if args[:3] == ("inspect", "--format", "{{json .State}}"):
            return _result(*args, out='{"Status":"running","OOMKilled":false}')
        if args[0] == "logs":
            return _result(*args, out="FATAL:  the database system is starting up")
        if args[:2] == ("rm", "-f"):
            return _result(*args, out="removed")
        return _result(*args, out="{}")

    def refuse(*args, **kwargs):
        raise pg_chaos.psycopg.OperationalError("the database system is starting up")

    clock = iter(range(0, 10_000, 200))
    monkeypatch.setattr(pg_chaos, "_docker", fake_docker)
    monkeypatch.setattr(pg_chaos.psycopg, "connect", refuse)
    monkeypatch.setattr(pg_chaos.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pg_chaos.time, "sleep", lambda seconds: None)
    fixture = pg_chaos.isolated_postgres_service.__wrapped__(
        _server_request(SimpleNamespace(DSN=None, CONTAINER_ID=None)), monkeypatch, tmp_path,
    )
    with pytest.raises(pytest.fail.Exception) as failure:
        next(fixture)
    message = str(failure.value)
    assert "never became ready" in message
    assert '"OOMKilled":false' in message
    assert "starting up" in message
    assert any(args[:2] == ("rm", "-f") for args in seen)


def test_successful_lifecycle_restores_module_and_env_and_removes_container(tmp_path: Path):
    """No module global or env var may survive the fixture into later tests."""
    seen = []
    module = SimpleNamespace(DSN="postgresql://shared/sose", CONTAINER_ID="shared-id")
    patch = pytest.MonkeyPatch()
    patch.setenv("SOSE_TEST_POSTGRES_DSN", "postgresql://shared/sose")
    patch.setenv("SOSE_POSTGRES_CONTAINER_ID", "shared-id")
    fixture_patch = pytest.MonkeyPatch()
    with pytest.MonkeyPatch.context() as mocks:
        mocks.setattr(pg_chaos, "_docker", _healthy_docker(seen))
        mocks.setattr(pg_chaos.psycopg, "connect", lambda *a, **k: _Connection())
        mocks.setattr(pg_chaos, "_postmaster_start_time", lambda dsn: "t0")
        fixture = pg_chaos.isolated_postgres_service.__wrapped__(
            _server_request(module), fixture_patch, tmp_path,
        )
        service = next(fixture)
        assert service.dsn == "postgresql://postgres@127.0.0.1:55501/postgres"
        assert module.DSN == service.dsn != "postgresql://shared/sose"
        assert module.CONTAINER_ID == "container-id"
        assert os.environ["SOSE_TEST_POSTGRES_DSN"] == service.dsn
        with pytest.raises(StopIteration):
            next(fixture)
    fixture_patch.undo()
    try:
        assert module.DSN == "postgresql://shared/sose"
        assert module.CONTAINER_ID == "shared-id"
        assert os.environ["SOSE_TEST_POSTGRES_DSN"] == "postgresql://shared/sose"
        assert os.environ["SOSE_POSTGRES_CONTAINER_ID"] == "shared-id"
        removals = [args for args in seen if args[:2] == ("rm", "-f")]
        assert len(removals) == 1 and removals[0][2].startswith("sose-pg-chaos-")
        image_check = seen.index(("image", "inspect", pg_chaos.ISOLATED_POSTGRES_IMAGE))
        run = next(i for i, args in enumerate(seen) if args[0] == "run")
        assert image_check < run and seen[run][-1] == pg_chaos.ISOLATED_POSTGRES_IMAGE
    finally:
        patch.undo()


@pytest.mark.parametrize("after", ("restarted", "unreachable"))
def test_destructive_test_that_disturbs_shared_postgres_fails(tmp_path: Path, after: str):
    seen = []
    module = SimpleNamespace(DSN="postgresql://shared/sose", CONTAINER_ID="shared-id")
    calls = iter(("t0", after))

    def start_time(dsn):
        assert dsn == "postgresql://shared/sose"
        value = next(calls)
        if value == "unreachable":
            raise pg_chaos.psycopg.OperationalError("connection refused")
        return value

    fixture_patch = pytest.MonkeyPatch()
    with pytest.MonkeyPatch.context() as mocks:
        mocks.setenv("SOSE_TEST_POSTGRES_DSN", "postgresql://shared/sose")
        mocks.setattr(pg_chaos, "_docker", _healthy_docker(seen))
        mocks.setattr(pg_chaos.psycopg, "connect", lambda *a, **k: _Connection())
        mocks.setattr(pg_chaos, "_postmaster_start_time", start_time)
        fixture = pg_chaos.isolated_postgres_service.__wrapped__(
            _server_request(module), fixture_patch, tmp_path,
        )
        next(fixture)
        expected = "restarted the shared" if after == "restarted" else "shared PostgreSQL unreachable"
        with pytest.raises(pytest.fail.Exception, match=expected):
            next(fixture)
        fixture_patch.undo()
    # The dedicated container is still removed when the shared check fails.
    assert sum(args[:2] == ("rm", "-f") for args in seen) == 1


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

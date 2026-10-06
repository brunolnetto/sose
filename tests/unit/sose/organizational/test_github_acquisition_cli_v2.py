from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sose.organizational.github_acquisition_cli_v2 import run


class _FakeClient:
    def __init__(self, *, token: str | None = None) -> None:
        self.token = token


def test_cli_delegates_explicit_batch_without_selecting_cohort(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def client_factory(*, token: str | None = None) -> _FakeClient:
        client = _FakeClient(token=token)
        observed["client"] = client
        return client

    def acquire_batch(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    output = tmp_path / "tranche.json"
    result = run(
        [
            "--repository",
            "brunolnetto/sose",
            "--prs",
            "302,303,304",
            "--captured-at",
            "2026-10-06T18:30:00Z",
            "--output",
            str(output),
        ],
        environ={"GITHUB_TOKEN": "secret-token"},
        client_factory=client_factory,
        acquire_batch=acquire_batch,
    )

    assert result == 0
    assert isinstance(observed["client"], _FakeClient)
    assert observed["client"].token == "secret-token"
    assert observed["repository"] == "brunolnetto/sose"
    assert observed["pr_numbers"] == (302, 303, 304)
    assert observed["captured_at"] == datetime(2026, 10, 6, 18, 30, tzinfo=timezone.utc)
    assert observed["output_path"] == output


def test_cli_reads_token_from_named_environment_variable(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def client_factory(*, token: str | None = None) -> _FakeClient:
        observed["token"] = token
        return _FakeClient(token=token)

    result = run(
        [
            "--repository",
            "brunolnetto/sose",
            "--prs",
            "302,303",
            "--captured-at",
            "2026-10-06T18:30:00+00:00",
            "--output",
            str(tmp_path / "tranche.json"),
            "--token-env",
            "SOSE_GITHUB_TOKEN",
        ],
        environ={"SOSE_GITHUB_TOKEN": "alternate-secret"},
        client_factory=client_factory,
        acquire_batch=lambda **_: object(),
    )

    assert result == 0
    assert observed["token"] == "alternate-secret"


def test_cli_strips_whitespace_from_token_before_constructing_client(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def client_factory(*, token: str | None = None) -> _FakeClient:
        observed["token"] = token
        return _FakeClient(token=token)

    result = run(
        [
            "--repository",
            "brunolnetto/sose",
            "--prs",
            "302,303",
            "--captured-at",
            "2026-10-06T18:30:00Z",
            "--output",
            str(tmp_path / "tranche.json"),
        ],
        environ={"GITHUB_TOKEN": "  mounted-secret\n"},
        client_factory=client_factory,
        acquire_batch=lambda **_: object(),
    )

    assert result == 0
    assert observed["token"] == "mounted-secret"


def test_cli_rejects_missing_token_before_acquisition(tmp_path: Path) -> None:
    called = False

    def acquire_batch(**_: object) -> object:
        nonlocal called
        called = True
        return object()

    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        run(
            [
                "--repository",
                "brunolnetto/sose",
                "--prs",
                "302,303",
                "--captured-at",
                "2026-10-06T18:30:00Z",
                "--output",
                str(tmp_path / "tranche.json"),
            ],
            environ={},
            client_factory=_FakeClient,
            acquire_batch=acquire_batch,
        )

    assert called is False


def test_cli_rejects_naive_capture_timestamp(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run(
            [
                "--repository",
                "brunolnetto/sose",
                "--prs",
                "302,303",
                "--captured-at",
                "2026-10-06T18:30:00",
                "--output",
                str(tmp_path / "tranche.json"),
            ],
            environ={"GITHUB_TOKEN": "secret"},
            client_factory=_FakeClient,
            acquire_batch=lambda **_: object(),
        )


def test_cli_rejects_malformed_or_duplicate_pr_lists(tmp_path: Path) -> None:
    base = [
        "--repository",
        "brunolnetto/sose",
        "--captured-at",
        "2026-10-06T18:30:00Z",
        "--output",
        str(tmp_path / "tranche.json"),
    ]

    for value in ("", "302", "302,302", "302,nope", "0,302"):
        with pytest.raises(ValueError, match="pull request"):
            run(
                [*base, "--prs", value],
                environ={"GITHUB_TOKEN": "secret"},
                client_factory=_FakeClient,
                acquire_batch=lambda **_: object(),
            )


def test_cli_does_not_expose_token_to_acquisition_arguments(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def acquire_batch(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    run(
        [
            "--repository",
            "brunolnetto/sose",
            "--prs",
            "302,303",
            "--captured-at",
            "2026-10-06T18:30:00Z",
            "--output",
            str(tmp_path / "tranche.json"),
        ],
        environ={"GITHUB_TOKEN": "never-persist-me"},
        client_factory=_FakeClient,
        acquire_batch=acquire_batch,
    )

    assert "token" not in observed
    assert "never-persist-me" not in repr(observed)

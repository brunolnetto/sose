from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
import os
from pathlib import Path

from .github_acquisition_batch_v2 import acquire_and_publish_github_pr_batch_v2
from .github_acquisition_v2 import UrllibGitHubJsonClient


ClientFactory = Callable[..., object]
AcquireBatch = Callable[..., object]


def run(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: ClientFactory = UrllibGitHubJsonClient,
    acquire_batch: AcquireBatch = acquire_and_publish_github_pr_batch_v2,
) -> int:
    """Acquire one explicit prospective Stage-1 tranche.

    This entrypoint intentionally performs no cohort discovery, eligibility
    selection, model fitting, freeze, or Stage-2 enrollment. It only turns an
    explicit PR-number tranche into the existing complete acquisition artifact.
    """

    parser = _parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    repository = arguments.repository.strip()
    if not repository or "/" not in repository:
        raise ValueError("repository must be owner/name")

    pr_numbers = _parse_pr_numbers(arguments.prs)
    captured_at = _parse_captured_at(arguments.captured_at)

    environment = os.environ if environ is None else environ
    token_env = arguments.token_env.strip()
    token = environment.get(token_env)
    if token is None or not token.strip():
        raise ValueError(f"missing GitHub token in environment variable {token_env}")

    client = client_factory(token=token)
    acquire_batch(
        repository=repository,
        pr_numbers=pr_numbers,
        client=client,
        captured_at=captured_at,
        output_path=Path(arguments.output),
    )
    return 0


def main() -> int:
    return run()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sose.organizational.github_acquisition_cli_v2",
        description="Acquire one explicit prospective v2 Stage-1 evidence tranche.",
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--prs", required=True, help="Comma-separated pull request numbers")
    parser.add_argument("--captured-at", required=True, help="Timezone-aware ISO-8601 timestamp")
    parser.add_argument("--output", required=True)
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    return parser


def _parse_pr_numbers(value: str) -> tuple[int, ...]:
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("pull request list must contain integers") from exc
    if len(numbers) < 2:
        raise ValueError("pull request list requires at least two pull requests")
    if any(number < 1 for number in numbers):
        raise ValueError("pull request numbers must be positive")
    if len(set(numbers)) != len(numbers):
        raise ValueError("pull request list must contain distinct pull requests")
    return numbers


def _parse_captured_at(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        captured_at = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("captured-at must be ISO-8601") from exc
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured-at must be timezone-aware")
    return captured_at


if __name__ == "__main__":
    raise SystemExit(main())

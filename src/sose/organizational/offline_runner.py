from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .empirical_pilot import GitHubPRObservationSnapshot
from .preregistration import (
    PRReviewEmpiricalPlan,
    PRReviewPreregisteredPilotResult,
    execute_pr_review_empirical_plan,
)


def run_pr_review_empirical_files(
    *,
    snapshot_path: str | Path,
    plan_path: str | Path,
    output_path: str | Path,
) -> PRReviewPreregisteredPilotResult:
    """Run a PR-review empirical plan entirely from local serialized artifacts.

    This function performs no fetching. Source acquisition and credential handling
    stay outside the organizational model and outside the reproducible execution.
    Publication is atomic and refuses to replace a different existing artifact.
    """

    snapshot_file = Path(snapshot_path)
    plan_file = Path(plan_path)
    output_file = Path(output_path)

    snapshot = GitHubPRObservationSnapshot.model_validate_json(
        snapshot_file.read_text(encoding="utf-8")
    )
    plan = PRReviewEmpiricalPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    execution = execute_pr_review_empirical_plan(snapshot=snapshot, plan=plan)
    serialized = execution.canonical_json() + "\n"

    output_file.parent.mkdir(parents=True, exist_ok=True)
    _publish_exclusive(output_file, serialized)
    return execution


def _publish_exclusive(output_file: Path, serialized: str) -> None:
    """Atomically publish a complete artifact without overwriting another result."""

    if output_file.exists():
        _accept_identical_or_raise(output_file, serialized)
        return

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_file.parent,
        prefix=f".{output_file.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output_file)
        except FileExistsError:
            _accept_identical_or_raise(output_file, serialized)
    finally:
        temporary.unlink(missing_ok=True)


def _accept_identical_or_raise(output_file: Path, serialized: str) -> None:
    if output_file.read_text(encoding="utf-8") == serialized:
        return
    raise FileExistsError(f"output already contains a different artifact: {output_file}")

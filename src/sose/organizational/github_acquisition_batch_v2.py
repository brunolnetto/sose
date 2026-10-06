from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tempfile

from .acquisition_v2 import GitHubPRAcquisitionSnapshotV2
from .github_acquisition_v2 import GitHubJsonClient, acquire_github_pr_v2


def acquire_github_pr_batch_v2(
    *,
    repository: str,
    pr_numbers: tuple[int, ...],
    client: GitHubJsonClient,
    captured_at: datetime,
) -> GitHubPRAcquisitionSnapshotV2:
    """Acquire one all-or-nothing prospective evidence tranche."""

    unique_numbers = tuple(dict.fromkeys(pr_numbers))
    if len(unique_numbers) < 2 or len(unique_numbers) != len(pr_numbers):
        raise ValueError("batch acquisition requires at least two distinct pull requests")
    if any(number < 1 for number in unique_numbers):
        raise ValueError("pull request numbers must be positive")

    artifacts = tuple(
        acquire_github_pr_v2(
            repository=repository,
            pr_number=number,
            client=client,
            captured_at=captured_at,
        )
        for number in unique_numbers
    )
    return GitHubPRAcquisitionSnapshotV2(artifacts=artifacts)


def acquire_and_publish_github_pr_batch_v2(
    *,
    repository: str,
    pr_numbers: tuple[int, ...],
    client: GitHubJsonClient,
    captured_at: datetime,
    output_path: str | Path,
) -> GitHubPRAcquisitionSnapshotV2:
    """Acquire a complete tranche before atomically publishing any artifact."""

    snapshot = acquire_github_pr_batch_v2(
        repository=repository,
        pr_numbers=pr_numbers,
        client=client,
        captured_at=captured_at,
    )
    _publish_exclusive(Path(output_path), snapshot.canonical_json() + "\n")
    return snapshot


def _publish_exclusive(output_file: Path, serialized: str) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
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

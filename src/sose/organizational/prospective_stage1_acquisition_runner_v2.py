from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .github_acquisition_batch_v2 import acquire_and_publish_github_pr_batch_v2
from .prospective_stage1_runner_v2 import run_stage1_tranche_files_v2


AcquireBatch = Callable[..., object]
AdvanceFiles = Callable[..., object]


@dataclass(frozen=True, slots=True)
class ProspectiveStage1AcquisitionRunV2:
    tranche_path: Path
    cumulative_acquisition_path: Path
    state_path: Path
    checkpoint_path: Path
    advancement: object


def run_stage1_acquisition_v2(
    *,
    repository: str,
    pr_numbers: tuple[int, ...],
    captured_at: datetime,
    protocol_path: str | Path,
    output_dir: str | Path,
    client: object,
    previous_acquisition_path: str | Path | None = None,
    previous_state_path: str | Path | None = None,
    acquire_batch: AcquireBatch = acquire_and_publish_github_pr_batch_v2,
    advance_files: AdvanceFiles = run_stage1_tranche_files_v2,
) -> ProspectiveStage1AcquisitionRunV2:
    """Acquire and advance one explicit prospective Stage-1 tranche.

    This orchestration intentionally exposes no cohort discovery, model fitting,
    model freeze, or Stage-2 behavior. The preregistered Stage-1 runner remains
    authoritative for enrollment and readiness semantics.
    """

    if (previous_acquisition_path is None) != (previous_state_path is None):
        raise ValueError("previous acquisition and previous state must be supplied together")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")

    destination = Path(output_dir)
    tranche_path = destination / "tranche-acquisition.json"
    cumulative_path = destination / "cumulative-acquisition.json"
    state_path = destination / "prospective-state.json"
    checkpoint_path = destination / "stage1-readiness.json"
    _preflight_outputs((tranche_path, cumulative_path, state_path, checkpoint_path))

    protocol = Path(protocol_path)
    if not protocol.is_file():
        raise FileNotFoundError(f"protocol artifact not found: {protocol}")

    previous_acquisition = (
        Path(previous_acquisition_path) if previous_acquisition_path is not None else None
    )
    previous_state = Path(previous_state_path) if previous_state_path is not None else None
    for previous in (previous_acquisition, previous_state):
        if previous is not None and not previous.is_file():
            raise FileNotFoundError(f"previous Stage-1 artifact not found: {previous}")

    destination.mkdir(parents=True, exist_ok=True)
    acquire_batch(
        repository=repository,
        pr_numbers=pr_numbers,
        client=client,
        captured_at=captured_at,
        output_path=tranche_path,
    )
    advancement = advance_files(
        protocol_path=protocol,
        tranche_acquisition_path=tranche_path,
        cumulative_acquisition_output_path=cumulative_path,
        state_output_path=state_path,
        checkpoint_output_path=checkpoint_path,
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
    )
    return ProspectiveStage1AcquisitionRunV2(
        tranche_path=tranche_path,
        cumulative_acquisition_path=cumulative_path,
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        advancement=advancement,
    )


def _preflight_outputs(paths: tuple[Path, ...]) -> None:
    if len(paths) != len(set(paths)):
        raise ValueError("Stage-1 acquisition output paths must be distinct")
    for path in paths:
        if path.exists():
            raise FileExistsError(f"output already exists: {path}")

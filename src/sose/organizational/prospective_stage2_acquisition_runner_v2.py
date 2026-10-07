from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import tempfile

from .acquisition_v2 import GitHubPRAcquisitionSnapshotV2
from .github_acquisition_batch_v2 import acquire_and_publish_github_pr_batch_v2
from .prospective_model_freeze_v2 import ProspectiveModelFreezeArtifactV2
from .prospective_stage2_runner_v2 import (
    ProspectiveStage2AdvancementV2,
    ProspectiveStage2CheckpointV2,
    advance_stage2_artifacts_v2,
    build_stage2_checkpoint_v2,
)
from .prospective_state_v2 import ProspectiveEvidenceStateV2


AcquireBatch = Callable[..., object]
AdvanceFiles = Callable[..., object]
ValidatePrevious = Callable[..., object]


@dataclass(frozen=True, slots=True)
class ProspectiveStage2AcquisitionRunV2:
    tranche_path: Path
    cumulative_acquisition_path: Path
    state_path: Path
    checkpoint_path: Path
    advancement: object


def run_stage2_acquisition_v2(
    *,
    repository: str,
    pr_numbers: tuple[int, ...],
    captured_at: datetime,
    model_freeze_path: str | Path,
    previous_acquisition_path: str | Path,
    previous_state_path: str | Path,
    output_dir: str | Path,
    client: object,
    previous_checkpoint_path: str | Path | None = None,
    acquire_batch: AcquireBatch = acquire_and_publish_github_pr_batch_v2,
    advance_files: AdvanceFiles | None = None,
    validate_previous: ValidatePrevious | None = None,
) -> ProspectiveStage2AcquisitionRunV2:
    """Acquire and advance one explicit Stage-2 tranche.

    Cohort classification remains authoritative in the prospective state machine.
    This orchestration performs no cohort discovery, fitting, prediction, or validation.
    """

    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    if not pr_numbers:
        raise ValueError("at least one explicit pull request number is required")
    if len(pr_numbers) != len(set(pr_numbers)):
        raise ValueError("duplicate pull request numbers are not allowed")

    freeze = Path(model_freeze_path)
    previous_acquisition = Path(previous_acquisition_path)
    previous_state = Path(previous_state_path)
    previous_checkpoint = (
        Path(previous_checkpoint_path) if previous_checkpoint_path is not None else None
    )
    for path in (freeze, previous_acquisition, previous_state, previous_checkpoint):
        if path is not None and not path.is_file():
            raise FileNotFoundError(f"required Stage-2 artifact not found: {path}")

    destination = Path(output_dir)
    tranche_path = destination / "tranche-acquisition.json"
    cumulative_path = destination / "cumulative-acquisition.json"
    state_path = destination / "prospective-state.json"
    checkpoint_path = destination / "stage2-checkpoint.json"
    _preflight_outputs((tranche_path, cumulative_path, state_path, checkpoint_path))

    validator = _validate_previous_bundle if validate_previous is None else validate_previous
    validator(
        model_freeze_path=freeze,
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
        previous_checkpoint_path=previous_checkpoint,
    )

    destination.mkdir(parents=True, exist_ok=True)
    acquire_batch(
        repository=repository,
        pr_numbers=pr_numbers,
        client=client,
        captured_at=captured_at,
        output_path=tranche_path,
    )
    advance = run_stage2_tranche_files_v2 if advance_files is None else advance_files
    advancement = advance(
        model_freeze_path=freeze,
        tranche_acquisition_path=tranche_path,
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
        previous_checkpoint_path=previous_checkpoint,
        cumulative_acquisition_output_path=cumulative_path,
        state_output_path=state_path,
        checkpoint_output_path=checkpoint_path,
    )
    return ProspectiveStage2AcquisitionRunV2(
        tranche_path=tranche_path,
        cumulative_acquisition_path=cumulative_path,
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        advancement=advancement,
    )


def run_stage2_tranche_files_v2(
    *,
    model_freeze_path: str | Path,
    tranche_acquisition_path: str | Path,
    previous_acquisition_path: str | Path,
    previous_state_path: str | Path,
    cumulative_acquisition_output_path: str | Path,
    state_output_path: str | Path,
    checkpoint_output_path: str | Path,
    previous_checkpoint_path: str | Path | None = None,
) -> ProspectiveStage2AdvancementV2:
    """Advance and atomically publish one Stage-2 tranche from local artifacts."""

    freeze = ProspectiveModelFreezeArtifactV2.model_validate_json(
        Path(model_freeze_path).read_text(encoding="utf-8")
    )
    tranche = GitHubPRAcquisitionSnapshotV2.model_validate_json(
        Path(tranche_acquisition_path).read_text(encoding="utf-8")
    )
    previous_acquisition = GitHubPRAcquisitionSnapshotV2.model_validate_json(
        Path(previous_acquisition_path).read_text(encoding="utf-8")
    )
    previous_state = ProspectiveEvidenceStateV2.model_validate_json(
        Path(previous_state_path).read_text(encoding="utf-8")
    )
    previous_checkpoint = (
        ProspectiveStage2CheckpointV2.model_validate_json(
            Path(previous_checkpoint_path).read_text(encoding="utf-8")
        )
        if previous_checkpoint_path is not None
        else None
    )

    result = advance_stage2_artifacts_v2(
        model_freeze=freeze,
        tranche=tranche,
        previous_acquisition=previous_acquisition,
        previous_state=previous_state,
        previous_checkpoint=previous_checkpoint,
    )

    publications = (
        (Path(cumulative_acquisition_output_path), result.acquisition.canonical_json() + "\n"),
        (Path(state_output_path), result.state.canonical_json() + "\n"),
        (Path(checkpoint_output_path), result.checkpoint.canonical_json() + "\n"),
    )
    _preflight_publications(publications)
    for path, serialized in publications:
        _publish_exclusive(path, serialized)
    return result


def _validate_previous_bundle(
    *,
    model_freeze_path: Path,
    previous_acquisition_path: Path,
    previous_state_path: Path,
    previous_checkpoint_path: Path | None,
) -> None:
    freeze = ProspectiveModelFreezeArtifactV2.model_validate_json(
        model_freeze_path.read_text(encoding="utf-8")
    )
    acquisition = GitHubPRAcquisitionSnapshotV2.model_validate_json(
        previous_acquisition_path.read_text(encoding="utf-8")
    )
    state = ProspectiveEvidenceStateV2.model_validate_json(
        previous_state_path.read_text(encoding="utf-8")
    )
    if acquisition.to_evidence_snapshot().snapshot_hash != state.snapshot_hash:
        raise ValueError("previous acquisition and state must represent the same evidence snapshot")

    expected = build_stage2_checkpoint_v2(state=state, model_freeze=freeze)

    if state.snapshot_hash == freeze.snapshot_hash:
        if state.previous_state_hash != freeze.training_state_hash:
            raise ValueError("freeze artifact does not bind to exact pre-freeze state")
        if previous_checkpoint_path is not None:
            raise ValueError("first Stage-2 advancement must not supply a previous checkpoint")
        return

    if previous_checkpoint_path is None:
        raise ValueError("previous Stage-2 checkpoint is required after Stage 2 has started")
    supplied = ProspectiveStage2CheckpointV2.model_validate_json(
        previous_checkpoint_path.read_text(encoding="utf-8")
    )
    if supplied.checkpoint_hash != expected.checkpoint_hash:
        raise ValueError("previous Stage-2 checkpoint does not match previous state")


def _preflight_outputs(paths: tuple[Path, ...]) -> None:
    if len(paths) != len(set(paths)):
        raise ValueError("Stage-2 acquisition output paths must be distinct")
    for path in paths:
        if path.exists():
            raise FileExistsError(f"output already exists: {path}")


def _preflight_publications(publications: tuple[tuple[Path, str], ...]) -> None:
    paths = tuple(path for path, _ in publications)
    if len(paths) != len(set(paths)):
        raise ValueError("Stage-2 output paths must be distinct")
    for path, serialized in publications:
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise FileExistsError(f"output already contains a different artifact: {path}")


def _publish_exclusive(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") == serialized:
            return
        raise FileExistsError(f"output already contains a different artifact: {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != serialized:
                raise
    finally:
        temporary.unlink(missing_ok=True)

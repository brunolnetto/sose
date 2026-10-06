from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from .acquisition_union_v2 import merge_acquisition_snapshots_v2
from .acquisition_v2 import GitHubPRAcquisitionSnapshotV2
from .prospective_protocol_v2 import ProspectiveStudyProtocolV2
from .prospective_stage1_v2 import (
    ProspectiveStage1ReadinessCheckpointV2,
    build_stage1_readiness_checkpoint_v2,
)
from .prospective_state_v2 import (
    ProspectiveEvidenceStateV2,
    advance_prospective_evidence_state_v2,
)


@dataclass(frozen=True, slots=True)
class ProspectiveStage1AdvancementV2:
    acquisition: GitHubPRAcquisitionSnapshotV2
    state: ProspectiveEvidenceStateV2
    checkpoint: ProspectiveStage1ReadinessCheckpointV2


def advance_stage1_artifacts_v2(
    *,
    protocol: ProspectiveStudyProtocolV2,
    tranche: GitHubPRAcquisitionSnapshotV2,
    previous_acquisition: GitHubPRAcquisitionSnapshotV2 | None = None,
    previous_state: ProspectiveEvidenceStateV2 | None = None,
) -> ProspectiveStage1AdvancementV2:
    """Advance preregistered Stage 1 without network access or model freeze."""

    if (previous_acquisition is None) != (previous_state is None):
        raise ValueError("previous acquisition and previous state must be supplied together")

    cumulative = (
        tranche
        if previous_acquisition is None
        else merge_acquisition_snapshots_v2(previous_acquisition, tranche)
    )
    state = advance_prospective_evidence_state_v2(
        records=cumulative.to_evidence_snapshot().records,
        protocol=protocol,
        previous_state=previous_state,
        model_frozen_at=None,
    )
    checkpoint = build_stage1_readiness_checkpoint_v2(state)
    return ProspectiveStage1AdvancementV2(
        acquisition=cumulative,
        state=state,
        checkpoint=checkpoint,
    )


def run_stage1_tranche_files_v2(
    *,
    protocol_path: str | Path,
    tranche_acquisition_path: str | Path,
    cumulative_acquisition_output_path: str | Path,
    state_output_path: str | Path,
    checkpoint_output_path: str | Path,
    previous_acquisition_path: str | Path | None = None,
    previous_state_path: str | Path | None = None,
) -> ProspectiveStage1AdvancementV2:
    """Advance and publish one Stage-1 tranche from immutable local artifacts.

    All result objects are computed and every destination is preflighted before the
    first write. Publication is individually atomic and idempotent; rerunning after
    an interrupted publication safely fills any missing identical artifacts.
    """

    if (previous_acquisition_path is None) != (previous_state_path is None):
        raise ValueError("previous acquisition and previous state paths must be supplied together")

    protocol = ProspectiveStudyProtocolV2.model_validate_json(
        Path(protocol_path).read_text(encoding="utf-8")
    )
    tranche = GitHubPRAcquisitionSnapshotV2.model_validate_json(
        Path(tranche_acquisition_path).read_text(encoding="utf-8")
    )
    previous_acquisition = (
        GitHubPRAcquisitionSnapshotV2.model_validate_json(
            Path(previous_acquisition_path).read_text(encoding="utf-8")
        )
        if previous_acquisition_path is not None
        else None
    )
    previous_state = (
        ProspectiveEvidenceStateV2.model_validate_json(
            Path(previous_state_path).read_text(encoding="utf-8")
        )
        if previous_state_path is not None
        else None
    )

    result = advance_stage1_artifacts_v2(
        protocol=protocol,
        tranche=tranche,
        previous_acquisition=previous_acquisition,
        previous_state=previous_state,
    )

    publications = (
        (Path(cumulative_acquisition_output_path), result.acquisition.canonical_json() + "\n"),
        (Path(state_output_path), result.state.canonical_json() + "\n"),
        (Path(checkpoint_output_path), result.checkpoint.canonical_json() + "\n"),
    )
    _preflight_publications(publications)
    for output_file, serialized in publications:
        _publish_exclusive(output_file, serialized)
    return result


def _preflight_publications(publications: tuple[tuple[Path, str], ...]) -> None:
    paths = tuple(path for path, _ in publications)
    if len(paths) != len(set(paths)):
        raise ValueError("Stage-1 output paths must be distinct")
    for output_file, serialized in publications:
        if output_file.exists() and output_file.read_text(encoding="utf-8") != serialized:
            raise FileExistsError(f"output already contains a different artifact: {output_file}")


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

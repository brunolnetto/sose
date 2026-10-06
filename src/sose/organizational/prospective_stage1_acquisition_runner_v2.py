from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .github_acquisition_batch_v2 import acquire_and_publish_github_pr_batch_v2
from .prospective_runner_v2 import publish_prospective_protocol_binding_v2
from .prospective_stage1_runner_v2 import run_stage1_tranche_files_v2


BindProtocol = Callable[..., object]
AcquireBatch = Callable[..., object]
AdvanceFiles = Callable[..., object]


@dataclass(frozen=True, slots=True)
class ProspectiveStage1AcquisitionRunV2:
    protocol_path: Path
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
    protocol_document_path: str | Path,
    registration_merged_at: datetime,
    output_dir: str | Path,
    client: object,
    previous_acquisition_path: str | Path | None = None,
    previous_state_path: str | Path | None = None,
    bind_protocol: BindProtocol = publish_prospective_protocol_binding_v2,
    acquire_batch: AcquireBatch = acquire_and_publish_github_pr_batch_v2,
    advance_files: AdvanceFiles = run_stage1_tranche_files_v2,
) -> ProspectiveStage1AcquisitionRunV2:
    """Bind protocol, acquire, and advance one explicit prospective Stage-1 tranche.

    This orchestration intentionally exposes no cohort discovery, model fitting,
    model freeze, or Stage-2 behavior. The preregistered Stage-1 runner remains
    authoritative for enrollment and readiness semantics.
    """

    if (previous_acquisition_path is None) != (previous_state_path is None):
        raise ValueError("previous acquisition and previous state must be supplied together")
    for name, value in (
        ("captured_at", captured_at),
        ("registration_merged_at", registration_merged_at),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    destination = Path(output_dir)
    bound_protocol_path = destination / "bound-protocol.json"
    tranche_path = destination / "tranche-acquisition.json"
    cumulative_path = destination / "cumulative-acquisition.json"
    state_path = destination / "prospective-state.json"
    checkpoint_path = destination / "stage1-readiness.json"
    _preflight_outputs(
        (bound_protocol_path, tranche_path, cumulative_path, state_path, checkpoint_path)
    )

    protocol_document = Path(protocol_document_path)
    if not protocol_document.is_file():
        raise FileNotFoundError(f"protocol document not found: {protocol_document}")

    previous_acquisition = (
        Path(previous_acquisition_path) if previous_acquisition_path is not None else None
    )
    previous_state = Path(previous_state_path) if previous_state_path is not None else None
    for previous in (previous_acquisition, previous_state):
        if previous is not None and not previous.is_file():
            raise FileNotFoundError(f"previous Stage-1 artifact not found: {previous}")

    destination.mkdir(parents=True, exist_ok=True)
    bind_protocol(
        protocol_document_path=protocol_document,
        registration_merged_at=registration_merged_at,
        output_path=bound_protocol_path,
    )
    acquire_batch(
        repository=repository,
        pr_numbers=pr_numbers,
        client=client,
        captured_at=captured_at,
        output_path=tranche_path,
    )
    advancement = advance_files(
        protocol_path=bound_protocol_path,
        tranche_acquisition_path=tranche_path,
        cumulative_acquisition_output_path=cumulative_path,
        state_output_path=state_path,
        checkpoint_output_path=checkpoint_path,
        previous_acquisition_path=previous_acquisition,
        previous_state_path=previous_state,
    )
    return ProspectiveStage1AcquisitionRunV2(
        protocol_path=bound_protocol_path,
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

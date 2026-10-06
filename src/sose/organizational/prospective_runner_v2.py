from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from .prospective_protocol_v2 import (
    ProspectiveStudyProtocolV2,
    bind_pr_review_validation_protocol_v2,
)
from .prospective_state_v2 import (
    ProspectiveEvidenceStateV2,
    advance_prospective_evidence_state_v2,
)
from .source_evidence_v2 import GitHubPREvidenceSnapshotV2


def publish_prospective_protocol_binding_v2(
    *,
    protocol_document_path: str | Path,
    registration_merged_at: datetime,
    output_path: str | Path,
) -> ProspectiveStudyProtocolV2:
    """Bind and atomically publish the frozen v2 preregistration artifact.

    Network acquisition of the registration merge timestamp remains outside this
    runner. Re-running with identical inputs is idempotent; a different artifact
    is never allowed to replace an already published binding.
    """

    document_file = Path(protocol_document_path)
    output_file = Path(output_path)
    document = json.loads(document_file.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("prospective protocol document must be a JSON object")

    binding = bind_pr_review_validation_protocol_v2(
        document=document,
        registration_merged_at=registration_merged_at,
    )
    _publish_exclusive(output_file, binding.canonical_json() + "\n")
    return binding


def run_prospective_evidence_files_v2(
    *,
    protocol_path: str | Path,
    snapshot_path: str | Path,
    output_path: str | Path,
    previous_state_path: str | Path | None = None,
    model_frozen_at: datetime | None = None,
) -> ProspectiveEvidenceStateV2:
    """Advance the prospective v2 evidence chain from local immutable artifacts.

    This function performs no GitHub access. The caller supplies a bound protocol,
    a complete canonical source snapshot, and optionally the immediately preceding
    state artifact. Publication is atomic and refuses to overwrite a different
    result.
    """

    protocol = ProspectiveStudyProtocolV2.model_validate_json(
        Path(protocol_path).read_text(encoding="utf-8")
    )
    snapshot = GitHubPREvidenceSnapshotV2.model_validate_json(
        Path(snapshot_path).read_text(encoding="utf-8")
    )
    previous_state = (
        ProspectiveEvidenceStateV2.model_validate_json(
            Path(previous_state_path).read_text(encoding="utf-8")
        )
        if previous_state_path is not None
        else None
    )

    state = advance_prospective_evidence_state_v2(
        records=snapshot.records,
        protocol=protocol,
        model_frozen_at=model_frozen_at,
        previous_state=previous_state,
    )
    _publish_exclusive(Path(output_path), state.canonical_json() + "\n")
    return state


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

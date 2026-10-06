from __future__ import annotations

from .acquisition_v2 import GitHubPRAcquisitionArtifactV2, GitHubPRAcquisitionSnapshotV2


def merge_acquisition_snapshots_v2(
    previous: GitHubPRAcquisitionSnapshotV2,
    tranche: GitHubPRAcquisitionSnapshotV2,
) -> GitHubPRAcquisitionSnapshotV2:
    """Append a complete acquisition tranche without rewriting prior evidence.

    An identity that appears in both snapshots is accepted only when its canonical
    acquisition artifact is byte-semantically identical.  The previous artifact is
    retained in the resulting snapshot so advancing Stage-1 never refreshes or
    silently rewrites already-observed source evidence.
    """

    previous_repositories = {artifact.evidence.repository for artifact in previous.artifacts}
    tranche_repositories = {artifact.evidence.repository for artifact in tranche.artifacts}
    if len(previous_repositories) != 1 or len(tranche_repositories) != 1:
        raise ValueError("each acquisition snapshot must belong to one repository")
    if previous_repositories != tranche_repositories:
        raise ValueError("acquisition snapshots must belong to the same repository")

    by_key: dict[tuple[str, int], GitHubPRAcquisitionArtifactV2] = {
        _key(artifact): artifact for artifact in previous.artifacts
    }
    for artifact in tranche.artifacts:
        key = _key(artifact)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = artifact
            continue
        if existing.canonical_json() != artifact.canonical_json():
            raise ValueError(
                f"cannot rewrite previously acquired pull request {key[0]}#{key[1]}"
            )

    return GitHubPRAcquisitionSnapshotV2(artifacts=tuple(by_key.values()))


def _key(artifact: GitHubPRAcquisitionArtifactV2) -> tuple[str, int]:
    return artifact.evidence.repository, artifact.evidence.pr_number

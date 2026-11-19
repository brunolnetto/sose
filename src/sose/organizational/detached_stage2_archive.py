"""Read-only verification of the detached PR-review Stage-2 historical archive.

No fit, acquisition, model freeze, protocol mutation or scientific promotion.
Run: python -m sose.organizational.detached_stage2_archive
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_ARCHIVE = (
    Path(__file__).resolve().parents[3]
    / "docs/organizational/evidence/archive/pr-review-v2-stage2-detached-20261024"
)
_SOURCE_COMMIT = "f7bda7c51fecdf40d337e8bc8b850e4dc32b17d5"
_VERSION = "sose-detached-stage2-evidence/v1"
_MANIFEST_BLOB_SHA = "9c5a1e11a2ce890cea0c4e44a5966ca1b876f280"


def _load(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _git_blob_sha(data: bytes) -> str:
    prefix = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(prefix + data).hexdigest()


def verify_detached_stage2_archive(root: Path = _ARCHIVE) -> dict[str, object]:
    """Verify original Git objects and explicit prospective non-admission.

    A SHA-pinned manifest authenticates bytes against the original detached
    Git tree; it does not authenticate external observational claims.
    """
    manifest, entries = _verified_manifest(root)
    recorded: set[str] = set()
    for entry in entries:
        _verify_entry(root, manifest, entry, recorded)

    existing = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.json") if p.name != "manifest.json"
    }
    if existing != recorded:
        raise ValueError("detached archive contains missing or unregistered JSON")

    _verify_checkpoint_chain(root)
    _verify_latest_alias(root)
    return {
        "source_commit": _SOURCE_COMMIT,
        "git_blobs_verified": len(entries),
        "observed": 5,
        "target": 12,
        "validation_allowed": False,
        "status": "historical_incomplete_not_admitted",
    }


def _verified_manifest(root: Path) -> tuple[dict[str, Any], list[Any]]:
    manifest_bytes = (root / "manifest.json").read_bytes()
    if _git_blob_sha(manifest_bytes) != _MANIFEST_BLOB_SHA:
        raise ValueError("detached archive manifest does not match pinned immutable identity")
    manifest = json.loads(manifest_bytes)
    if (manifest.get("archive_version") != _VERSION
            or manifest.get("source_commit") != _SOURCE_COMMIT
            or manifest.get("validation_allowed") is not False
            or manifest.get("holdout_target") != 12):
        raise ValueError("detached archive metadata is not the registered source")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 15:
        raise ValueError("detached archive requires exactly 15 pinned files")
    return manifest, entries


def _valid_archive_path(name: Any, rel: Path, recorded: set[str]) -> bool:
    return not (
        not isinstance(name, str) or rel.is_absolute()
        or ".." in rel.parts or rel.as_posix() != name
        or name in recorded
        or not name.startswith(("checkpoint-004/", "checkpoint-005/", "latest/"))
    )


def _verify_entry(
    root: Path, manifest: dict[str, Any], entry: dict[str, Any], recorded: set[str]
) -> None:
    name = entry["path"]
    rel = Path(name)
    if not _valid_archive_path(name, rel, recorded):
        raise ValueError("invalid or duplicate detached archive path")
    recorded.add(name)
    if entry["source_path"] != manifest["origin_root"] + "/" + name:
        raise ValueError("source path does not match archived object")
    file = root / rel
    if file.is_symlink() or not file.is_file():
        raise ValueError("missing or linked detached evidence file")
    content = file.read_bytes()
    if len(content) != entry["size"] or _git_blob_sha(content) != entry["git_blob_sha"]:
        raise ValueError("detached evidence bytes differ from pinned Git object")


def _verify_checkpoint_chain(root: Path) -> None:
    previous = None
    for name, observed in (("checkpoint-004", 4), ("checkpoint-005", 5)):
        checkpoint = _load(root / name / "stage2-checkpoint.json")
        if not _unpromotable_checkpoint(checkpoint, observed):
            raise ValueError("checkpoint cannot be promoted to scientific validation")
        if previous is not None and checkpoint.get("previous_state_hash") != previous:
            raise ValueError("detached checkpoint state chain is broken")
        previous = checkpoint["state_hash"]


def _unpromotable_checkpoint(checkpoint: dict[str, Any], observed: int) -> bool:
    return (
        checkpoint.get("holdout_target") == 12
        and checkpoint.get("holdout_observed") == observed
        and checkpoint.get("holdout_remaining") == 12 - observed
        and checkpoint.get("complete") is False
        and checkpoint.get("validation_allowed") is False
    )


def _verify_latest_alias(root: Path) -> None:
    for field in ("cumulative-acquisition.json", "prospective-state.json",
                  "source.json", "stage2-checkpoint.json", "tranche-acquisition.json"):
        if (root / "latest" / field).read_bytes() != (
                root / "checkpoint-005" / field).read_bytes():
            raise ValueError("latest alias differs from final immutable checkpoint")


if __name__ == "__main__":
    print(json.dumps(verify_detached_stage2_archive(), sort_keys=True))

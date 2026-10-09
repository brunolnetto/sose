from __future__ import annotations

from pathlib import Path

import pytest

from scripts.organizational_official_run import (
    ANCHORS,
    REFERENCES,
    _git_blob,
    run_official,
    verify_freeze,
)


def test_official_runner_has_distinct_trusted_freeze_manifest_anchors() -> None:
    assert set(ANCHORS) == set(REFERENCES) == {"o2c", "mro"}
    assert len(set(ANCHORS.values())) == 2
    assert all(len(value) == 40 for value in ANCHORS.values())


def test_git_blob_id_does_not_confuse_content_and_git_object() -> None:
    assert _git_blob(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert _git_blob(b"a") != _git_blob(b"b")


@pytest.mark.parametrize("domain", ["o2c", "mro"])
def test_no_world_may_start_without_trusted_freeze_files(
    tmp_path: Path, domain: str,
) -> None:
    with pytest.raises(FileNotFoundError):
        verify_freeze(root=tmp_path, domain=domain)
    with pytest.raises(FileNotFoundError):
        run_official(root=tmp_path, domain=domain, output_dir=tmp_path / "never-published")
    assert not (tmp_path / "never-published").exists()

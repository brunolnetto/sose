from pathlib import Path

import pytest

from scripts.check_release_candidate import project_version, validate


def _fixture(tmp_path: Path, *, version: str = "1.2.3") -> Path:
    (tmp_path / "src" / "sose").mkdir(parents=True)
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "sose"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "sose" / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    return tmp_path


def test_project_version_reads_package_metadata(tmp_path):
    root = _fixture(tmp_path)
    assert project_version(root) == "1.2.3"


def test_manual_candidate_validation_does_not_require_completed_release_note(tmp_path):
    root = _fixture(tmp_path)
    validate(root, tag=None)


def test_tagged_candidate_requires_matching_version_and_completed_note(tmp_path):
    root = _fixture(tmp_path)
    (root / "docs" / "releases" / "v1.2.3.md").write_text(
        "# release\n",
        encoding="utf-8",
    )
    validate(root, tag="v1.2.3")


def test_tagged_candidate_rejects_version_mismatch(tmp_path):
    root = _fixture(tmp_path)
    with pytest.raises(SystemExit, match="release tag mismatch"):
        validate(root, tag="v9.9.9")


def test_tagged_candidate_rejects_missing_completed_release_note(tmp_path):
    root = _fixture(tmp_path)
    with pytest.raises(SystemExit, match="missing completed release note"):
        validate(root, tag="v1.2.3")


def test_repository_is_self_consistent_release_candidate():
    root = Path(__file__).resolve().parents[1]
    version = project_version(root)
    validate(root, tag=f"v{version}")

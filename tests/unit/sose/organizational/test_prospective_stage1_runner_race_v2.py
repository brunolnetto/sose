from pathlib import Path

import pytest

from sose.organizational.prospective_stage1_runner_v2 import _publish_exclusive


def test_publish_exclusive_revalidates_existing_different_artifact(tmp_path: Path) -> None:
    output = tmp_path / "artifact.json"
    output.write_text("other\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="different artifact"):
        _publish_exclusive(output, "expected\n")

    assert output.read_text(encoding="utf-8") == "other\n"


def test_publish_exclusive_accepts_existing_identical_artifact(tmp_path: Path) -> None:
    output = tmp_path / "artifact.json"
    output.write_text("expected\n", encoding="utf-8")

    _publish_exclusive(output, "expected\n")

    assert output.read_text(encoding="utf-8") == "expected\n"

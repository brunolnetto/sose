from pathlib import Path
import tomllib

import sose


def test_runtime_version_matches_project_metadata():
    pyproject = tomllib.loads(
        Path("pyproject.toml").read_text(encoding="utf-8")
    )

    assert sose.__version__ == "0.7.0"
    assert pyproject["project"]["version"] == sose.__version__

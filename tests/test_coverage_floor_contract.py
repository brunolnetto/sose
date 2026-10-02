from __future__ import annotations

from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
MINIMUM_RATCHET = 92.0


def test_coverage_floor_cannot_regress_below_ratchet():
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    configured = float(config["tool"]["coverage"]["report"]["fail_under"])

    workflow = CI.read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^\s*run:\s*coverage report --fail-under=(\d+(?:\.\d+)?)\s*$",
        workflow,
    )
    assert match is not None, "CI coverage floor command disappeared"
    enforced = float(match.group(1))

    assert configured >= MINIMUM_RATCHET
    assert enforced >= MINIMUM_RATCHET
    assert enforced == configured

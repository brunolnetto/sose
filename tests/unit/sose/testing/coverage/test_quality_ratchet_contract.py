"""ADR-0010: the structural quality ratchet cannot be loosened silently."""
from __future__ import annotations

import json
import re
import tomllib

from tests.support.paths import repo_root_from

ROOT = repo_root_from(__file__)
PYPROJECT = ROOT / "pyproject.toml"
CI = ROOT / ".github" / "workflows" / "ci.yml"

# ADR-0010 limits. Tightening is allowed; loosening needs a superseding ADR.
MAX_CC = 10
MAX_LINES = 400
MIN_MI = 20
GATES = {"max_cc", "max_lines", "min_mi"}


def _gates() -> dict:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["codebase-stats"]["gates"]


def test_quality_limits_are_not_looser_than_adr_0010():
    gates = _gates()
    assert gates["max-cc"] <= MAX_CC
    assert gates["max-lines"] <= MAX_LINES
    assert gates["min-mi"] >= MIN_MI


def test_baseline_is_versioned_and_only_records_known_gates():
    baseline_path = ROOT / _gates()["baseline"]
    assert baseline_path.is_file(), "quality baseline missing"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert isinstance(baseline, dict) and baseline
    for key, value in baseline.items():
        gate, _, subject = key.partition(":")
        assert gate in GATES, key
        assert subject, key
        assert isinstance(value, (int, float)), key
        # A baseline entry records a violation; a value within the limit
        # would be a stale entry that the strict gate must have removed.
        if gate == "max_cc":
            assert value > MAX_CC, key
        elif gate == "max_lines":
            assert value > MAX_LINES, key
        else:
            assert value < MIN_MI, key


def test_ci_runs_the_strict_ratchet_on_the_whole_package():
    workflow = CI.read_text(encoding="utf-8")
    gate = re.search(r"(?m)^\s*codebase-stats --root src/sose\b.*--strict-baseline", workflow)
    assert gate is not None, "CI no longer runs the strict quality ratchet"
    assert "--exclude" not in gate.group(0)
    # The pipe into tee must not swallow the gate's exit status.
    assert re.search(r"set -o pipefail\s*\n(?:\s*#.*\n)*\s*codebase-stats --root src/sose", workflow)


def test_codebase_stats_is_pinned_to_one_minor_series():
    text = PYPROJECT.read_text(encoding="utf-8")
    pins = re.findall(r'"codebase-stats([^"]*)"', text)
    assert pins and all(pin == ">=0.1,<0.2" for pin in pins), pins

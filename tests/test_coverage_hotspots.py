from __future__ import annotations

import json

import pytest

from scripts.coverage_hotspots import main, render_markdown, summarize_coverage


def _payload():
    return {
        "files": {
            "src/alpha.py": {
                "missing_lines": [10, 11],
                "missing_branches": [[5, 10], [5, 12]],
                "summary": {"percent_covered": 80.0},
            },
            "src/beta.py": {
                "missing_lines": [7, 9, 12],
                "missing_branches": [],
                "summary": {"percent_covered": 70.0},
            },
            "src/complete.py": {
                "missing_lines": [],
                "missing_branches": [],
                "summary": {"percent_covered": 100.0},
            },
        }
    }


def test_hotspots_rank_combined_line_and_branch_debt():
    rows = summarize_coverage(_payload())

    assert [row["path"] for row in rows] == ["src/alpha.py", "src/beta.py"]
    assert rows[0]["total_debt"] == 4
    assert rows[0]["missing_branches"] == 2
    assert rows[1]["total_debt"] == 3


def test_hotspot_markdown_is_stable_and_explicit_about_metric():
    report = render_markdown(summarize_coverage(_payload()), limit=1)

    assert "`src/alpha.py`" in report
    assert "`src/beta.py`" not in report
    assert "prioritization metric" in report


def test_hotspots_reject_malformed_files_payload():
    with pytest.raises(ValueError, match="files object"):
        summarize_coverage({"files": []})


def test_cli_writes_report(tmp_path):
    source = tmp_path / "coverage.json"
    output = tmp_path / "hotspots.md"
    source.write_text(json.dumps(_payload()), encoding="utf-8")

    assert main([str(source), "--output", str(output), "--limit", "2"]) == 0
    assert "src/alpha.py" in output.read_text(encoding="utf-8")

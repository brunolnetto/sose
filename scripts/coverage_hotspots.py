from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path


def _branch_debt(file_payload: dict[str, object]) -> int:
    missing = file_payload.get("missing_branches", ())
    return len(missing) if isinstance(missing, list) else 0


def summarize_coverage(payload: dict[str, object]) -> list[dict[str, object]]:
    files = payload.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("coverage payload must contain a files object")

    rows: list[dict[str, object]] = []
    for path, raw in files.items():
        if not isinstance(path, str) or not isinstance(raw, dict):
            continue
        missing_lines = raw.get("missing_lines", ())
        line_debt = len(missing_lines) if isinstance(missing_lines, list) else 0
        branch_debt = _branch_debt(raw)
        if line_debt == 0 and branch_debt == 0:
            continue

        summary = raw.get("summary", {})
        percent = (
            float(summary.get("percent_covered", 0.0))
            if isinstance(summary, dict)
            else 0.0
        )
        rows.append(
            {
                "path": path,
                "missing_lines": line_debt,
                "missing_branches": branch_debt,
                "total_debt": line_debt + branch_debt,
                "percent_covered": percent,
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row["total_debt"]),
            -int(row["missing_branches"]),
            str(row["path"]),
        )
    )
    return rows


def _markdown_code(value: object) -> str:
    escaped = escape(str(value), quote=True)
    escaped = escaped.replace("|", "&#124;").replace("`", "&#96;")
    return "`" + escaped + "`"


def render_markdown(rows: list[dict[str, object]], *, limit: int = 30) -> str:
    selected = rows[:limit]
    lines = [
        "# Coverage hotspots",
        "",
        "| Rank | File | Missing lines | Missing branches | Total debt | Coverage |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for rank, row in enumerate(selected, start=1):
        lines.append(
            "| "
            f"{rank} | {_markdown_code(row['path'])} | "
            f"{row['missing_lines']} | "
            f"{row['missing_branches']} | "
            f"{row['total_debt']} | "
            f"{float(row['percent_covered']):.2f}% |"
        )
    if not selected:
        lines.append("| 1 | _none_ | 0 | 0 | 0 | 100.00% |")
    lines.append("")
    lines.append(
        "Debt is the count of uncovered executable lines plus uncovered branch arcs. "
        "It is a prioritization metric, not a substitute for behavioral review."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rank residual coverage debt from coverage.py JSON output."
    )
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be >= 1")

    payload = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    report = render_markdown(summarize_coverage(payload), limit=args.limit)
    if args.output is None:
        print(report, end="")
    else:
        args.output.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

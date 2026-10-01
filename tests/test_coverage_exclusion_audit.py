from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "sose"
LEDGER = ROOT / "docs" / "testing" / "coverage-exclusions.md"
LEDGER_ENTRY = re.compile(r"`(?P<path>[^\`]+\.py):(?P<line>\d+)`")


def _production_pragmas() -> dict[str, str]:
    pragmas: dict[str, str] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if "# pragma: no cover" not in line:
                continue
            pragmas[f"{relative}:{line_number}"] = line.strip()
    return pragmas


def _ledger_entries() -> tuple[str, ...]:
    return tuple(
        f"{match.group('path')}:{match.group('line')}"
        for match in LEDGER_ENTRY.finditer(LEDGER.read_text(encoding="utf-8"))
    )


def test_every_production_no_cover_pragma_is_audited_exactly_once():
    actual = set(_production_pragmas())
    entries = _ledger_entries()
    documented = set(entries)
    duplicates = sorted(
        {entry for entry in entries if entries.count(entry) > 1}
    )

    assert duplicates == [], (
        "coverage exclusion ledger contains duplicate audited entries: "
        f"{duplicates}"
    )
    assert actual == documented, (
        "coverage exclusion ledger drifted; add/remove a pragma only with an "
        "explicit audited ledger entry\n"
        f"undocumented={sorted(actual - documented)}\n"
        f"stale_ledger_entries={sorted(documented - actual)}"
    )


def test_every_production_no_cover_pragma_names_its_invariant_inline():
    missing_reason = {
        location: line
        for location, line in _production_pragmas().items()
        if re.search(r"# pragma: no cover -\\s*\\S", line) is None
    }

    assert missing_reason == {}, (
        "every production coverage exclusion must name its invariant inline: "
        f"{missing_reason}"
    )

# ADR-0010 — Codebase quality ratchet anchored on codebase-stats

- **Status:** Proposed
- **Date:** 2026-11-13
- **Decision owners:** SOSE maintainers
- **Related PRD(s):** none (library hygiene, no product capability)
- **Related TRD(s):** none
- **Supersedes:** none
- **Superseded by:** none

## Context

SOSE has an enforced coverage floor (95% branch-aware, `docs/testing/coverage-contract.md`),
but no enforced structural quality criterion. A `codebase-stats` 0.1.0 snapshot of
`src/sose` at `main@d3a70e9` found 244 violations of common structural limits:

- 162 functions above cyclomatic complexity (CC) 10. 45 of them are above 25, and the
  maximum is 126 in `composition/trading_company_customer.py::_execute_intent`.
- 51 modules above 400 lines. The largest has 1,906 lines.
- 31 modules with a maintainability index (MI) below 20. Three are at 0.0.

The worst offenders are the same files that hold the coverage deficit tracked in #590,
and the same functions that dominate the runtime profile of the slowest PostgreSQL tests.
Without a gate, every change can make them worse unnoticed.

The project is pausing feature/issue work to sanitise the library. That work needs a
criterion that blocks regressions immediately and can be tightened over time.

## Decision

1. **Tool.** `codebase-stats` (pinned `>=0.1,<0.2`) is the source of the structural
   metrics. CC and MI come from radon; module size is lines per file.
2. **Limits.** These are the tool's defaults, configured in `[tool.codebase-stats.gates]`
   of `pyproject.toml`:
   - `max-cc = 10` per function or method;
   - `max-lines = 400` per module;
   - `min-mi = 20` per module.
3. **Scope.** All of `src/sose`. `examples/` is included because the canonical domains
   are part of the published package and of the coverage denominator.
4. **Ratchet.** Today's violations are recorded in `.codebase-stats-baseline.json`.
   CI fails on:
   - any new violation;
   - any baselined violation that gets worse;
   - any stale baseline entry (`--strict-baseline`). A fixed offender must therefore be
     removed from the baseline in the same change.

   The baseline may only shrink. Adding or loosening an entry requires an explicit
   superseding decision.
5. **Coverage.** The 95% branch-aware floor stays in coverage.py
   (`coverage report --fail-under=95`). `codebase-stats` additionally publishes the
   coverage-gap × complexity ranking in the coverage job, to decide which refactors need
   tests first.
6. **Enforcement point.** A dedicated `quality` CI job (no test run, fast feedback) runs
   the strict gate; it is the only enforcement point. The coverage job's analysis step is
   informational: with `--coverage`, codebase-stats 0.1.0 resolves a file without
   functions to another file of the same basename and reports phantom violations.
   While hosted Actions are unavailable (#586), the same job is reproduced locally from
   `ci.yml`.

## Decision drivers

- Block regressions now without blocking every PR on historical debt.
- Make the debt measurable and visible, so that it is paid down from the worst offenders.
- Keep one tool for the structural metrics and the coverage × complexity view.

## Consequences

### Positive

- New code must meet the limits, and existing hotspots can only improve.
- Fixing a hotspot is visible in review as removed baseline entries.

### Negative / trade-offs

- A change that legitimately touches a large baselined function, even to fix a bug,
  must not increase its CC or length. It may have to extract code first.
- The tool is pinned to a minor series. Upgrading it is a deliberate change, because
  metric or key changes can shift the baseline.
- The full gate takes about a minute, which is why it runs as its own CI job rather than
  in the unit suite.

### Constraints introduced

- `tests/unit/sose/testing/coverage/test_quality_ratchet_contract.py` fails if:
  - the limits are loosened;
  - the baseline goes missing or becomes malformed;
  - the CI job stops running the strict gate.

## Alternatives considered

### Alternative A — Hard limits immediately

Rejected. All 244 violations would block every PR until they were fixed, which turns
hygiene into a big-bang refactor.

### Alternative B — Report only

Rejected. Metrics that do not fail anything have not prevented the current debt.

### Alternative C — Looser limits (CC 15, 600 lines, MI 10)

Rejected. A smaller baseline is not worth a weaker bar for new code.

## Compatibility and migration

No runtime, API or persisted-data effect. The migration is configuration only:
- the tool pin;
- the `[tool.codebase-stats.gates]` table;
- the baseline file;
- the new CI job;
- the analysis step moved off flags that 0.1.0 deprecates for removal (positional
  coverage file, `--show`, `--source-root`).

## Verification

- `codebase-stats --root src/sose --only cc,size,mi --strict-baseline` exits 0 on this change.
- It exits 1 for a new function with CC 11.
- It exits 1 for a baselined function whose CC grows. Making `_execute_intent` 126 → 127
  is reported as "baseline 126, got worse", together with its module's line growth.
- The contract test above.

## References

- `CODEBASE_STATS_REPORT.md` snapshot (codebase-stats 0.1.0, `main@d3a70e9`).
- #590 (coverage ratchet), #606/#610 (performance evidence), #586 (hosted CI outage).

## Notes

Raising the bar (stricter limits, or `--min-coverage` in codebase-stats) is a later,
superseding decision once the baseline is small.

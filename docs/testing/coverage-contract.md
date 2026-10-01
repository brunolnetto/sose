# Coverage contract

SOSE treats coverage as executable evidence, not as a vanity percentage.

The target is **100% branch coverage of runtime production behavior that can be
meaningfully exercised**. The denominator includes the authoritative runtime,
all built-in domains/canonicals, persistence adapters shipped in the package, job
orchestration, sinks, scenarios, and public factories.

## Authoritative measurement

The CI `coverage` job runs the ordinary suite with a real PostgreSQL service and
also enables the process, fencing, and storage-chaos suites. Coverage.py's
subprocess patch instruments Python child workers; chaos workers checkpoint their
current coverage data before publishing a pause marker, so a subsequent SIGKILL
does not discard all evidence collected before the kill. This prevents the
coverage number from ignoring code that is already exercised in separate
conformance jobs.

The PostgreSQL whole-service infrastructure chaos job remains a separate gate
because it deliberately stops the service container. Its semantic evidence is
required, but it is not yet folded into the single-process coverage run.

## What may be excluded

An exclusion is acceptable only when the source construct has no meaningful
runtime execution path:

- `typing.Protocol` class bodies. They describe static interfaces; executing the
  ellipsis bodies is not product behavior.
- `TYPE_CHECKING`-only imports and declarations.
- defensive invariants explicitly marked `# pragma: no cover` when reaching them
  would require violating a guarantee already enforced by the constructor,
  database engine, or internal closed enum.

These exclusions are not permission to hide error paths. Validation failures,
retry paths, optional-dependency failures, malformed configuration, stale-writer
behavior, backend outages, and domain unhappy paths remain testable behavior and
stay in the coverage denominator.

## Not intrinsically untestable

The following may require specialized CI, mocks, subprocesses, or external
services, but are not classified as impossible:

- PostgreSQL behavior and connection failure;
- POSIX process death;
- disk-full/write-denial behavior;
- optional adapters such as DuckDB/DuckLake, ClickHouse, Databricks, and
  Snowflake;
- packaging behavior with missing optional dependencies;
- all domain state transitions and unhappy paths.

If a path cannot run in the default CI environment, it should receive a dedicated
test envelope rather than a blanket coverage exclusion.

## Convergence rule

Until the project reaches 100%, every coverage PR must do one of two things for
each remaining miss:

1. add executable evidence that reaches it; or
2. document why the path has no meaningful runtime execution and make the
   exclusion as narrow as possible.

Coverage thresholds are ratcheted upward as evidence is added; they never move
backward to make a change pass. The first authoritative combined baseline is
88.36%, so CI now requires at least 88%. Once the measured reachable set reaches
100%, `fail_under` is raised to 100 and any future uncovered branch becomes a
merge blocker.


## Codebase quality analysis

The authoritative coverage job also runs `codebase-stats` against the generated
`coverage.json` and pytest JSON report, with `src/sose` as the analysis root.

Coverage gaps are prioritized together with:

- cyclomatic complexity (CC);
- maintainability index (MI);
- comment/docstring ratio;
- Halstead bug and difficulty estimates;
- source file size and structural outliers;
- slow-test duration;
- explicit `# pragma: no cover` counts.

This matters because a low-coverage, low-complexity validation branch usually
needs a focused test, while a low-coverage file that is simultaneously large,
complex, and low-MI may need decomposition before more tests are added.

The generated `codebase-stats-report.txt`, `coverage.json`, and
`pytest-report.json` are retained as CI artifacts so coverage convergence can
be audited against code-quality trends rather than against a single percentage.

# Runtime performance benchmarks

Performance measurements are pytest-benchmark tests, separate from semantic
conformance and correctness gates. The benchmark workflow executes the
`benchmarks/pytest_benchmarks` suite and uploads one JSON report.

## Why timing is not a correctness gate

Shared CI runners vary by host generation, contention, virtualization, and
background load. Fixed wall-clock thresholds turn infrastructure noise into
false regressions. Pull requests run reduced workloads to validate execution;
the weekly/manual workflow runs full cardinalities. Compare distributions and
trends from equivalent environments, not absolute numbers across machines.

## Workloads

The suite retains the former runtime baseline scenarios:

- memory scheduled batches;
- SQLite snapshot, SQLite incremental, JSONL journal, and DuckDB scheduled
  batches;
- persistent adapter close/reopen and scheduled-work recovery;
- diagnostics collection;
- resource contention and priority-store selection.

The diagnostics microbenchmarks independently vary scheduled-work cardinality,
event-history cardinality, and command-index cardinality. Each end-to-end
workload constructs its state inside the measured callable, so setup remains
part of the end-to-end cost.

## Running

```bash
SOSE_BENCHMARK_MODE=quick uv run pytest -o addopts= benchmarks/pytest_benchmarks \
  --benchmark-only --benchmark-json=/tmp/sose-benchmarks.json
```

Set `SOSE_BENCHMARK_MODE=full` (or omit it) for full sizes. The JSON report
contains pytest-benchmark timing distributions and environment metadata.

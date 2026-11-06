# SOSE benchmarks

All executable performance benchmarks are pytest tests using
[pytest-benchmark](https://pytest-benchmark.readthedocs.io/). There are no
standalone timing scripts or parallel timing/reporting implementations.

## Run locally

Install development dependencies and run the isolated benchmark suite:

```bash
uv sync --group dev
SOSE_BENCHMARK_MODE=quick uv run pytest -o addopts= benchmarks/pytest_benchmarks \
  --benchmark-only --benchmark-json=/tmp/sose-benchmarks.json
```

Omit `SOSE_BENCHMARK_MODE=quick` for the full cardinalities. The explicit
`-o addopts=` disables the repository's automatic PostgreSQL fixture plugin:
these benchmark workloads use in-memory and local-file adapters and must not
start Docker or require a PostgreSQL service.

## Workload boundaries

- End-to-end runtime workloads include their full setup and teardown in each
  measured invocation, matching the former runtime baseline.
- Persistence scaling seeds durable entities once, closes and reopens the
  adapter, then measures one-record transactions. Setup is excluded.
- Command-existence benchmarks seed once and measure either the batch API or
  the legacy per-ID fallback. The two paths are checked for equivalent results
  before timing.
- Diagnostics cardinality tests continue to vary scheduled work, event history,
  and command-index size independently.

Quick mode is used for pull requests and reduces workload sizes and rounds.
Full mode is used for scheduled and manual runs. PostgreSQL is intentionally
excluded from local durable-adapter comparisons because its network/service
conditions require a separately controlled environment.

## Interpreting results

The JSON artifact is the single report format. Compare runs from equivalent
machines and software environments; do not treat shared GitHub-hosted runner
timings as latency SLAs or add brittle wall-clock pass/fail thresholds.

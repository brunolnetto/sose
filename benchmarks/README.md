# SOSE benchmarks

SOSE has two complementary benchmark layers:

- `runtime_baseline.py` and `persistence_scaling.py` measure complete
  scenarios and persistence scaling. They remain the end-to-end baseline.
- `pytest-benchmark` measures selected runtime operations in isolation. The
  first microbenchmark covers the read-only `Engine.diagnostics()` path over
  prepared in-memory state at two scheduled-work cardinalities.

## Run the microbenchmarks

Install the development dependencies, then run:

```bash
uv sync --group dev
uv run pytest \
  -o addopts= \
  benchmarks/pytest_benchmarks \
  --benchmark-only \
  --benchmark-json=/tmp/sose-pytest-benchmark.json
```

The explicit `-o addopts=` is important: the repository's normal pytest
configuration loads `tests.support.pytest_auto_postgres`, which probes for a
PostgreSQL server and may start a Docker container when none is available.
These microbenchmarks use only `MemoryPersistence`; clearing the configured
`addopts` keeps their execution independent of PostgreSQL and Docker.

The benchmark fixture prepares each runtime before timing begins. The measured
call is only `engine.diagnostics()`. The suite is outside pytest's configured
`tests/` discovery tree, so it does not run as part of the default test suite
or its coverage gate.

## Interpreting results

Use the JSON output to compare runs made on equivalent machines and software
environments. Do not treat timings from shared GitHub-hosted runners as stable
latency SLAs, and do not add hard pass/fail thresholds based on those runners.

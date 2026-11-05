# SOSE benchmarks

SOSE has two complementary benchmark layers:

- `runtime_baseline.py` and `persistence_scaling.py` measure complete
  scenarios and persistence scaling. They remain the end-to-end baseline.
- `pytest-benchmark` measures selected runtime operations in isolation. The
  diagnostics microbenchmarks vary scheduled-work cardinality, event-history
  cardinality, and command-index cardinality as separate workloads.

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

The benchmark fixtures prepare each runtime before timing begins. The measured
call is only `engine.diagnostics()`. The scheduled-work workload varies the
number of scheduled jobs; the event-history workload holds scheduled work and
commands fixed while adding events; the command-index workload holds scheduled
work fixed while adding unreferenced commands. The latter two deliberately
use the in-memory persistence state to construct isolated synthetic cardinality
profiles; they are diagnostic microbenchmarks, not persistence API examples.

The suite is outside pytest's configured `tests/` discovery tree, so it does
not run as part of the default test suite or its coverage gate.

## Interpreting results

Use the JSON output to compare runs made on equivalent machines and software
environments. Treat each workload as an independent scaling observation;
do not infer production adapter behavior from the in-memory results alone.
Do not treat timings from shared GitHub-hosted runners as stable latency SLAs,
and do not add hard pass/fail thresholds based on those runners.

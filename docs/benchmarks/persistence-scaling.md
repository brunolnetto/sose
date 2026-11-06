# Persistence scaling curves

The pytest-benchmark suite asks how transaction cost changes as durable state grows. For each sink and cardinality, it seeds N entities in one transaction, closes and reopens the adapter to discard setup caches, then measures single-entity update transactions. Setup is excluded from the timed region. The sink matrix remains snapshot SQLite, incremental SQLite, JSONL journal, and DuckDB; full cardinalities remain 10, 100, 1,000, and 10,000.

## Command existence

The same suite compares batch command-existence lookup with the legacy per-ID fallback in MemoryPersistence and each local durable adapter. It preserves the original lookup cardinalities and 80/20 present/missing mix. Seeding is excluded from timing, and result equivalence is asserted before measurement.

## CI and local execution

Pull requests use reduced sizes (10 and 100 entities; 1,000 seeded commands). Manual full runs and the weekly schedule use all cardinalities (through 10,000 entities; 10,000 seeded commands). The unified report is `pytest-benchmark-results.json`.

```bash
SOSE_BENCHMARK_MODE=full uv run pytest -o addopts= benchmarks/pytest_benchmarks \
  --benchmark-only --benchmark-json=/tmp/sose-benchmarks.json
```

Timing thresholds are not correctness gates. Inspect curve shape and compare runs collected in equivalent environments.

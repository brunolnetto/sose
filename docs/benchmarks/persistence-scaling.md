# Persistence scaling curves

The persistence scaling benchmark isolates one question:

> when exactly one durable record changes, how does transaction cost grow as the
> total durable state grows?

This is different from the throughput benchmark, which measures complete
scheduled/recovery workflows.

## Workload

For each sink and cardinality:

1. create N durable entities in one setup transaction;
2. close and reopen the sink so setup caches do not leak into measurement;
3. update exactly one entity per measured transaction;
4. repeat the update several times;
5. record min/median/max update latency and durable storage size.

The default full cardinalities are:

```text
10
100
1,000
10,000
```

The current sink matrix is:

- snapshot SQLite;
- incremental SQLite;
- JSONL journal;
- DuckDB.

Setup time is deliberately excluded from the measured update latency. The
experiment is about marginal mutation cost with pre-existing durable state.

## Why this benchmark matters

Snapshot SQLite is expected to pay for serializing/replacing the full state on
each transaction.

Dirty-tracked record sinks should trend toward cost proportional to the touched
records, but they may still retain whole-state costs from:

- UnitOfWork deepcopy;
- database record reload;
- journal replay;
- record-cache invalidation;
- backend-specific transaction overhead.

The curve therefore tests the architectural hypothesis more directly than one
fixed-size throughput number.

## CI vs full workflow

Ordinary CI runs:

```text
N = 10, 100
repetitions = 1
```

to verify that every sink and the output schema remain executable.

The manual/weekly benchmark workflow runs:

```text
N = 10, 100, 1,000, 10,000
repetitions = 5
```

Timing thresholds are still not correctness gates.

## Running locally

```bash
python benchmarks/persistence_scaling.py \
  --sizes 10,100,1000,10000 \
  --repetitions 5 \
  --output persistence-scaling.json
```

A subset can be selected explicitly:

```bash
python benchmarks/persistence_scaling.py \
  --sizes 100,1000 \
  --sinks sqlite_snapshot,sqlite_incremental \
  --repetitions 3
```

## Interpretation

Useful conclusions include:

- whether incremental SQLite overtakes snapshot SQLite as state grows;
- whether dirty tracking actually flattens the marginal-update curve;
- whether JSONL replay becomes dominant;
- whether DuckDB's analytical orientation makes small operational writes
  unsuitable;
- whether the common UnitOfWork deepcopy becomes the next dominant shared cost.

Do not compare one run across unrelated machines as if it were a product SLA.
Prefer curves collected by the same workflow/environment and inspect the shape,
not only one absolute latency.

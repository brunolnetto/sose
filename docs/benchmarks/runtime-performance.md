# Runtime performance benchmarks

SOSE performance benchmarks measure implementation cost. They are separate from
semantic conformance and correctness gates.

## Why timing is not a correctness gate

Shared CI runners vary by host generation, contention, virtualization, and
background load. A fixed wall-clock threshold would turn infrastructure noise
into false regressions.

Therefore:

- ordinary CI executes a reduced benchmark suite to prove the harness and
  workloads remain executable;
- CI uploads the resulting JSON as an artifact;
- a dedicated benchmark workflow runs the full suite manually and weekly;
- performance comparisons should use runs from the same workflow/environment
  and compare distributions/trends rather than one absolute number.

## Workloads

### memory_scheduled_batch

Creates many tutorial entities, transitions them to running, persists one
ScheduledWork boundary per entity, and executes all boundaries.

This measures the combined cost of deterministic identity, persistence
transactions, durable scheduling, StateChart dispatch, event creation, and
backend callback execution with MemoryPersistence.

### sqlite_scheduled_batch

Runs the same semantic workload through SQLitePersistence.

The current v0.8/v0.9 SQLite implementation stores a complete tagged semantic
snapshot per transaction, so this workload intentionally exposes the cost of
that correctness-first design.

### sqlite_reopen

Creates pending scheduled work in SQLite, closes the connection, opens a fresh
adapter, rebuilds a fresh backend, and verifies all scheduled ownership is
restored.

This measures the durability/recovery boundary rather than steady-state query
throughput.

### diagnostics_collection

Creates a larger pending scheduled runtime and collects
`Engine.diagnostics()`.

This tracks the cost of the pull-based durable observability surface.

### resource_contention

Queues more Resource requests than capacity permits and repeatedly releases
owners until all waiters are promoted and completed.

This measures durable contention bookkeeping and deterministic promotion.

### priority_store_selection

Populates a Priority Store and consumes all items through durable
`ensure_selection()` ownership.

This measures Store intent/result persistence and backend selection.

## Output

`benchmarks/runtime_baseline.py` emits versioned JSON containing:

- benchmark schema version;
- quick/full mode;
- Python version;
- platform/machine/processor;
- CPU count;
- item count;
- repetition count;
- min/median/max duration;
- median items/second.

Example:

```bash
python benchmarks/runtime_baseline.py --output benchmark-results.json
python benchmarks/runtime_baseline.py --quick
```

## Interpreting results

Benchmark numbers are not product SLAs.

Use them to answer questions such as:

- which subsystem dominates as durable cardinality increases?
- how much overhead does SQLite snapshot persistence add over memory?
- does a persistence refactor improve write cost while preserving conformance?
- does diagnostics scale acceptably with durable state size?
- is PostgreSQL work justified by measured external-persistence bottlenecks?

The benchmark suite should be extended when a new performance question appears;
it should not accumulate workloads without a decision they inform.

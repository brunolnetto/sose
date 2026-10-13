# PC6 concurrent PostgreSQL forensic reproduction (#468)

Status: **diagnostic instrument, not root-cause fix**. Do not close #468.

## Why

The real test `test_pg_two_real_customer_statecharts_overlap_without_business_aliasing`
has intermittently failed on exactly the same operational workflow under CI load,
with `unknown durable store item`, `backend logical time does not match persisted
recovery boundary`, and missing `shipment` errors.

Two independent PostgreSQL client connections working on independent customer
flows do not automatically imply isolated clock, stores or resource identity:
the existing `PostgresPersistence` record adapter explicitly supports
last-writer-wins on conflicting records, while process reference flows may use
a shared logical position and shared store definitions. Those are **candidate**
causes, not proven causes of this specific failure.

## Reproduce without silently turning a race into a sequential test

```bash
SOSE_TEST_POSTGRES_DSN='<separately supplied secure DSN>' \
python scripts/reproduce_pc6_pg_overlap.py \
  --runs 12 --parallel 3 --timeout 120 \
  --out /tmp/sose-pc6-overlap
```

Each of the `runs` launches a fresh pytest subprocess containing **two actual
parallel customer StateCharts and an in-transaction causal ingress barrier**.
Each pytest invocation uses a unique PostgreSQL namespace. Multiple subprocesses
run concurrently to recreate nontrivial real PostgreSQL scheduling pressure.

Outputs:
- `run-<id>.log` contains pytest traceback and—on worker exception—a read-only
  reopened committed-state diagnostic.
- `summary.json` lists all runs, pass/fail/timeout status and log path.
- A failure or timeout returns nonzero. **A pass is not evidence of absence of
  races**, and no automatic retry normalizes a failed run into a green result.

No credentials are written to the summary. The diagnostic snapshot lists only
bounded record IDs, lifecycle states and resource/store request representations.
It does not persist a new tick, correct divergence, or mutate domain state.

## Stop rule before production changes

1. Reproduce at least one original failure in the bounded harness and keep the
   exact first divergent committed snapshot alongside traceback.
2. Identify the first conflicting `collection, record_key` and compare client
   snapshots, logical positions, job scopes, owners and real overlap windows.
3. Determine: legitimate resource wait (#489); shared store identity; global
   `SimulationPosition` alias; stale record conflict; or another mechanism.
4. Add a **deterministic RED** interleaving test for the confirmed mechanism.
5. Fix with preserved real parallelism and per-owner causal truth; repeat
   PostgreSQL, multi-Python, chaos, coverage and historical replay gates.

This preparatory instrumentation never changes scientific frozen v1 sources,
#477 physical arbitration admission or the prospective #461 OD-5a protocol.

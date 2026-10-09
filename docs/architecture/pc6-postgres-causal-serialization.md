# PC6: PostgreSQL causal serialization (ADR / falsification protocol)

**Status:** experimental enforcement, subject to PostgreSQL integration and
chaos gates. This is one workstream in issue #406.

## Invariant

If a causal predecessor is not durably published, ACKed and (when staged as a
Command) applied, no descendant worker may commit its ACK. A boundary claim
must not read a partially committed predecessor graph or overwrite another
worker's claim as a last-writer-wins update.

A physical commit order is not inherently a causal order. For each message
edge `a -> b`, require durable evidence of `a` before making `b`
claimable, irrespective of timestamps or ID order.

## Decision

Use a **namespace-scoped PostgreSQL transactional advisory lock** for boundary
publish/claim/ACK transactions, acquired before loading the authoritative
UnitOfWork snapshot. This establishes a serial order for boundary state changes
while preserving the existing concurrent dirty-record transactions for
independent business entities.

- PostgreSQL `PostgresPersistence.boundary_transaction`: exclusive boundary
  advisory lock before nested standard OLTP transaction.
- `FencedEnginePersistence.boundary_transaction`: forwards authoritative
  writer fencing epoch without bypassing claim/publish ordering.
- `BoundaryService`: opts into semantic boundary transactions when available.
- Legacy Memory/SQLite adapters retain their ordinary durable UoW path.

## Falsification gate

1. Two independent connections share a namespace. Worker A begins publishing a
   parent but is deliberately paused before commit. Worker B attempts to claim
   a previously published, typed descendant. Worker B must **not** complete
   its claim until A's publication transaction commits.
2. Parent ACK with a pending durable business Command must not release its
   descendant to another worker.
3. After the business effect is applied, the descendant is claimable exactly
   once, with lease epoch fencing for stale claims.
4. Crash/partition tests must independently verify final causal equivalence
   between uninterrupted and restarted executions before closing #406.

## Trade-offs and non-goals

The serial boundary lock limits throughput per namespace and does not
serialize unrelated domain writes. It protects ordering of boundary mutations,
not every arbitrary external effect. End-to-end guarantees additionally need
authoritative business completion certificates and conflict-aware recovery.
The lock must not be held across external network calls or SimPy execution.

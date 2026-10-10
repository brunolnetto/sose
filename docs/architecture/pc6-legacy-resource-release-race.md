# PC6 — reservation-local reconciliation of legacy Engine releases

**Scope:** a regression discovered while validating #440; narrowly fixes an existing
`DurableResourceManager.release` vs `rebuild_backend` race. Complements,
but does not replace, authoritative temporal resources (#436/#438/#440).

## PRD

A worker releasing a legacy SimPy-backed resource must not lose its durable
release intent to another worker rebuilding the same PostgreSQL namespace.
Competing recovery should wait for the owner to finish, or apply the intent
after the owner's process really dies. Unrelated reservations must progress.

## TRD

The PG adapter's existing session-scoped `business_resource_guard` is
acquired with a **reservation-identity key**, over the full multi-commit
`release` lifecycle. Recovery of pending release intents acquires the
same scoped key **before refreshing** the persisted intent and reservation.
An intent that disappeared after acquiring the lock was finalized by its
owner and must not be replayed. An intent that was mutated incompatibly
still fails closed. No new global lock and no changes to the historical
Memory/SQLite single-worker path.

The lock does **not** own finite physical capacity. This patch only
coordinates legacy release protocol reconciliation; the PostgreSQL temporal
ledger remains the authoritative capacity/ownership source when mapped.

## ADR

Treat the legacy `ResourceReleaseIntent` as an intermediate recovery
artifact. Lock duration is restricted to a single reservation's release and
is not an organization-wide serialization point. A worker crash drops its
session lock automatically, preserving its already-committed release intent
for the next worker. This is transitional compatibility behavior, not a
proof of general multi-domain causal equivalence or external exactly-once.

## Falsification and promotion

- Two PostgreSQL connections: hold owner *after intent COMMIT*, while a
  second worker rebuilds/reconciles that very intent; no premature finalization,
  mismatch error or duplicate semantic release.
- An unrelated reservation can release while the first is suspended.
- Existing overlapping real PC6 statechart test remains strict and unchanged.
- Require full exact-head CI, dedicated PostgreSQL, Python 3.12–3.14,
  coverage >=95%, chaos, benchmark and frozen historical replays.
- Keep #406 open; never promote an ephemeral session lock as durable capacity.

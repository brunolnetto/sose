# v0.8 runtime hardening

## Purpose

The Reference suite proves domain semantics. The v0.8 hardening suite attacks a
different question:

> does durable truth remain deterministic when recovery and contention happen
> repeatedly or at higher cardinality than the narrative examples?

## Repeated rebuild invariant

Rebuilding an ephemeral backend at the same durable boundary must be
observationally idempotent.

For a pending scheduled transition:

```text
durable truth before rebuild #1
= durable truth after rebuild #1
= ...
= durable truth after rebuild #N
```

Only execution of a due semantic boundary may change durable truth.

`restart_reference_runtime_repeated()` standardizes this test mechanic without
hiding domain-specific assertions.

## Stale callback invariant

A callback/item captured before rebuild or before committed execution must not
replay a transition after its durable ScheduledWork ownership has been consumed.

The hardening suite executes the replacement runtime, then explicitly attempts
the stale item again and verifies that no second event is produced.

## Deterministic batch replay

Two independent runs with the same:

- entity keys;
- seed;
- logical time;
- command keys;
- scheduling order;

must produce equal durable entity snapshots, event history, and recovery
position even when dozens of items share the same due time.

## Resource contention stress

Equal-priority resource requests preserve durable request sequence across a
queue larger than resource capacity. Releasing current owners repeatedly must:

- promote waiters in deterministic order;
- leave no stranded demand/reservation;
- leave no release intent after successful completion.

## Store selection stress

A Priority Store with many items must select by priority and then stable
insertion/durable sequence. Every committed selection becomes terminal ownership
and no pending get request remains.

## Scope

These tests are intentionally bounded enough for ordinary CI. They are semantic
stress tests, not performance benchmarks. Performance/scalability claims require
separate measured benchmarks.

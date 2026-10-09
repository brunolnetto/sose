# PC6 — Persisted causal DAG audit (PRD / TRD / ADR)

## PRD

A fast ACK pipeline alone does not prove the event stream is causal. The PC6 operational truth needs an independently callable, deterministic *read-only auditor* that rejects typed predecessor gaps, cycles, order inversions and certificates unlinked from an accepted boundary. It must detect persisted pathologies after a worker restart without re-executing the simulation.

## TRD and canonical semantics

`audit_causal_history(persistence) -> CausalAuditReport` reads an authoritative transaction, using `boundary_transaction` on adapters that support causal serial order. It validates every durable boundary delivery/message identity, consumes ACK receipts only when status is `CONSUMED`, checks their effect IDs and immutable `BusinessEffectApplied` certificates, and validates referenced persisted entities' version floors.

Typed `BoundaryMessage.causation_kind=boundary` produces a directed edge. The referenced parent must exist with the same correlation and strictly earlier `produced_at`. Cycles are rejected independently of traversal order. Typed `event` predecessors require a durable domain event with consistent correlation and non-future occurrence time. Missing typed causes are failures even for pending, unclaimed deliveries.

**Frozen v1 compatibility:** an untyped `causation_id` is **opaque**, even if it resembles an event prefix or coincides with a boundary message ID. The audit counts opaque causes but never converts them to typed edges and never asserts a verified causal ancestry from them. A complete certified reference chain has a receipt only for contracts with specified terminal domain effects; other historical contracts retain their prior accepted-Command semantics.

**Normalized fingerprint:** stable SHA-256 over a sorted canonical representation of boundary message ID, contract, source, destination, correlation, explicit causation, payload hash and business-effect certificates (effect ID, entity identity, state, version). It intentionally excludes timestamps of worker observation, lease epochs, claim retries and job scheduler checkpoints. The fingerprint provides a business-causality equivalence target for later PostgreSQL kill/restart experiments, but is not a full byte-for-byte engine state hash.

## ADR and falsification

Use explicit typed edges for hard causal claims; do not infer ancestry from legacy plain text. Treat immutable certificates and delivery ACKs as separate semantic layers and inspect both. Detect lineage corruption before treating a recovered simulation as scientifically equivalent. Do not auto-correct corrupted state or silently fill missing parents.

The audit is deliberately read-only and validates one adapter transaction; it does **not** make out-of-band business side effects atomic or prove exactly-once delivery over network partitions. A complete PC6 promotion gate additionally requires running the same synthetic input against a continuous baseline and multiple PostgreSQL worker-death schedules and comparing fingerprints, committed entity/event ledgers, and duplicate-effect counts.

## TDD gate

Red-first unit and SQLite tests include cycles, missing typed predecessors, cross-correlation edges, temporal inversions, typed missing events, opaque v1 collisions, vanished certificates and deterministic fresh-process reconstruction. CI >=95% coverage, Python matrix, PostgreSQL and chaos gates, frozen organizational replay and resolved review are required.

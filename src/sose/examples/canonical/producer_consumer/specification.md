# Producer–Consumer

## Problem
A finite FIFO buffer separates producers from consumers. Producers may create more work than the buffer can admit immediately.

## SOSE mapping
- Buffer: durable `Store`
- Produced items: store items
- Consumers: durable selection requests
- Progress: `CanonicalCase` state chart (`ready -> active -> completed`)

## Parameters
`participants` controls produced/consumed items; `capacity` bounds the FIFO buffer; `enabled` can pause domain reconciliation between ticks.

## Executable invariants
- Store capacity is finite and configured by `capacity`.
- Consumer request identities are deterministic, so restart does not duplicate consumption.
- Continuous execution and execution interrupted by closing and reopening the SQLite-backed job converge to the same operational snapshot.

## Happy path
Items enter the bounded buffer, consumers obtain selections, and the case completes once every consumer has a durable result.

## Sad path
When producers exceed immediate buffer capacity, excess work remains governed by Store backpressure rather than bypassing capacity.

## Evidence
`tests/unit/sose/examples/canonical/test_canonical_conformance.py` proves restart equivalence. Catalog-wide dual-store and recurring-job tests also execute this example.

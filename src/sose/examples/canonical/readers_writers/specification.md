# Readers–Writers

## Problem
Multiple readers may access shared state concurrently, while writers require exclusive access.

## SOSE mapping
- Concurrent read capacity: `reader_slots` Resource
- Exclusive writer ownership: capacity-one `writer_gate`
- Readers and writers: deterministic durable requests

## Parameters
`participants` is total actors; `readers` must leave at least one writer; `enabled` controls reconciliation.

## Executable invariants
- `reader_slots.capacity == readers`, permitting concurrent reader ownership.
- `writer_gate.capacity == 1`, enforcing exclusive writer ownership.
- Durable ownership reconstructs after restart.
- Continuous and restarted execution converge to the same operational snapshot.

## Happy path
Readers can occupy reader slots concurrently; writer requests serialize through the writer gate.

## Sad path / limitation
The current canonical demonstrates durable shared-read capacity and exclusive writer gating, but it is not yet a formal proof of starvation freedom or a complete readers-preference/writers-preference algorithm.

## Evidence
Capacity invariants are asserted in `tests/test_canonical_examples.py`; restart equivalence is in `tests/test_canonical_conformance.py`.

# Dining Philosophers

## Problem
Adjacent philosophers contend for exclusive forks. Naive acquisition can create circular wait.

## SOSE mapping
- Each fork: capacity-one durable `Resource`
- Philosophers: deterministic paired resource requests
- Deadlock-prevention policy: globally ordered fork acquisition

## Parameters
`participants` determines both philosopher and fork count; `enabled` controls reconciliation.

## Executable invariants
- Exactly one capacity-one fork exists per philosopher.
- A fork cannot have multiple simultaneous owners.
- Fork identities are globally ordered before requests are issued, removing circular-wait ordering.
- Continuous and SQLite-reopened execution converge.

## Happy path
Contended fork reservations are progressively released until all demand drains.

## Sad path / limitation
This example demonstrates prevention by ordered acquisition; it does not intentionally enter and diagnose a deadlocked state.

## Evidence
Fork cardinality and capacity are asserted in `tests/unit/sose/examples/canonical/test_canonical_examples.py`; restart equivalence is asserted in `tests/unit/sose/examples/canonical/test_canonical_conformance.py`. Ordered-acquisition protocol coverage remains an explicit gap.

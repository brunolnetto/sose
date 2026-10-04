# Job Shop

## Problem
Jobs contend for a finite set of exclusive machines.

## SOSE mapping
- Machines: capacity-one durable `Resource` instances
- Jobs: deterministic operation requests
- Scheduling: contention handled by SOSE resource ownership

## Parameters
`participants` controls jobs; `machines` controls machine count; `enabled` controls reconciliation.

## Executable invariants
- Every machine has capacity one.
- A machine cannot own two simultaneous operations.
- Job and operation request identities are deterministic across restart.
- Continuous and restarted execution converge.

## Happy path
First operations contend for machines and reservations are progressively released until demand drains.

## Sad path / explicit gap
The current implementation models only `op-0` for each job. Multi-operation routing and precedence constraints are not yet implemented, so this example must not be used as evidence of full job-shop precedence correctness.

## Evidence
Restart equivalence is in `tests/unit/sose/examples/canonical/test_canonical_conformance.py`; generic Resource conformance covers exclusive machine ownership.

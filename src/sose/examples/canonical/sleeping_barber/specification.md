# Sleeping Barber

## Problem
Customers arrive at a barber with finite service capacity and a finite waiting room. Arrivals beyond those limits abandon the system.

## SOSE mapping
- Barber: durable `Resource`
- Waiting-room admission: bounded by `waiting_chairs + capacity`
- Customers: deterministic resource requests
- Progress: `CanonicalCase`

## Parameters
`participants`, `capacity`, `waiting_chairs`, and runtime-mutable `enabled`.

## Executable invariants
- At most `capacity` customers can own the barber resource concurrently.
- At most `waiting_chairs + capacity` arrivals are admitted.
- Resource leases survive reconstruction and are released through the Engine-owned resource manager.
- Continuous and restarted execution converge to the same operational snapshot.

## Happy path
Admitted customers queue, acquire service capacity, release it, and the case completes after all admitted demand drains.

## Sad path
Arrivals beyond service plus waiting capacity are deliberately not admitted, representing abandonment.

## Evidence
Restart equivalence is in `tests/test_canonical_conformance.py`; generic resource/restart gates exercise lease reconstruction.

# Engine OLTP persistence boundary

SOSE separates two persistence responsibilities.

## Engine OLTP

`EnginePersistence` stores the operational state required by the SOSE engine to
execute, stop, reconstruct, and deterministically continue a simulation. This
includes job state, simulation position, scheduled work, commands, resources,
stores, containers, pending intents/results, checkpoints, ownership/fencing
metadata, and the committed tick.

The existing `Persistence` and `UnitOfWork` names remain compatibility names.
New architecture and APIs should use `EnginePersistence` terminology.

## Domain warehouse

A domain warehouse stores the state of the simulated business/domain: for
example MRO inventory and purchase orders, manufacturing work orders, or credit
applications and loans.

A domain warehouse is not an Engine OLTP adapter merely because the same
database technology can implement both responsibilities. Its interface and
qualification criteria are separate.

## Boundary rule

Engine OLTP answers **"how does SOSE continue execution?"**.

Domain warehouse answers **"what is the current simulated world?"**.

Authoritative persistence qualification in this package applies only to the
Engine OLTP boundary.

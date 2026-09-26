# Maintenance / MRO example

> **Status: Partial implementation under the current reference-domain standard.**

The canonical human-readable contract and gap assessment is
[specification.md](specification.md).

The current executable example demonstrates a persistent WorkOrder StateChart and
durable scheduled lifecycle execution. It does not yet provide the complete
happy/sad-path vertical slice required for reference-grade status.

See the specification for:

- exact current StateChart semantics;
- what is already executable;
- durable scheduling guarantees already proven;
- missing technician/resource and spare-parts behavior;
- required happy/sad paths;
- restart-equivalence requirements for promotion.

# Maintenance / MRO example

> **Status: Reference implementation.**

The canonical human-readable contract and gap assessment is
[specification.md](specification.md).

The executable example now covers the canonical happy path plus representative
material-shortage, resource-contention, cancellation, emergency-preemption and
scenario paths, with domain-level restart-equivalence gates.

See the specification for:

- exact StateChart and process semantics;
- durable technician/bay and spare-parts ownership;
- happy and sad paths;
- emergency/preemption and scenario behavior;
- restart-equivalence guarantees and executable evidence.

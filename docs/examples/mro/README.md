# Maintenance / MRO reference domain

> **Canonical human-readable contract:** [specification.md](specification.md)
>
> The specification defines the executable MRO slice, exact StateCharts, persisted
> ERDs, happy/sad process semantics, invariants, durable ownership, scenarios, and
> restart contract. The remaining documents are supporting implementation notes.

## Status

**Reference implementation.**

## Canonical flow

```mermaid
flowchart LR
    A["WorkOrder(planned)"] --> B["released"]
    B --> C["in_progress"]
    C --> D["completed"]
    D --> E["closed"]
```

Operational prerequisites are explicit:

```mermaid
flowchart LR
    A["Spare part available"] --> B["Technician acquired"]
    B --> C["Maintenance bay acquired"]
    C --> D["Part lot + quantity consumed"]
    D --> E["Maintenance active"]
```

## Implemented reference surface

- durable WorkOrder and PartDemand lifecycles;
- spare-part shortage and replenishment;
- technician and maintenance-bay contention;
- explicit Store/Container part issue;
- cancellation before physical part issue;
- emergency bay preemption and resume;
- finite scenario interventions and expiry cleanup;
- deterministic restart equivalence across representative recovery boundaries.

## Happy and sad paths

- happy: release → acquire capacity → issue part → maintain → complete → close;
- sad: spare-part shortage → replenishment;
- sad: technician/bay contention → capacity recovery;
- sad: cancellation before part issue;
- sad: emergency preemption → interruption → reacquisition → resume;
- external: asset failure, spare-parts disruption, technician capacity loss.

A domain is not reference-grade if only its golden path is executable.

## Supporting documents

- [domain.md](domain.md) — entities, StateCharts, commands, invariants;
- [flow.md](flow.md) — operational flows and recovery boundaries;
- [mapping.md](mapping.md) — MRO concepts mapped to SOSE primitives;
- [scenarios.md](scenarios.md) — scenario semantics;
- [specification.md](specification.md) — canonical contract and executable evidence.

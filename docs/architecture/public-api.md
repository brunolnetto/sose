# Public API contract

## Purpose

SOSE v0.8 stabilizes the boundary that domain authors and persistence/backend
integrators can rely on.

The compatibility-relevant import surface is `sose.api`.

This document classifies library surfaces so future refactors can distinguish a
breaking API change from an internal implementation change.

## Public

The symbols exported by `sose.api.__all__` are the public domain-author and
integration contract for the v0.8 line.

They cover:

- logical clock/context/engine construction;
- deterministic randomness;
- domain entities and registry;
- probabilistic transition decorators;
- scenario definitions and effects;
- persistence protocols plus the in-memory adapter;
- backend-neutral temporal/resource/store/container protocols.

Prefer:

```python
from sose.api import (
    DomainRegistry,
    Engine,
    Entity,
    EntityType,
    MemoryPersistence,
    RandomSource,
    Scheduler,
    SimulationClock,
    SimulationContext,
    probabilistic_transitions,
)
```

over reaching into implementation modules for the same concepts.

### Compatibility expectation

Before 1.0, public symbols may still evolve at a minor release, but:

1. removal or incompatible signature changes require release-note migration
   guidance;
2. the public import must not disappear silently;
3. patch releases should preserve the public contract.

`tests/test_public_api_contract.py` is the executable anti-drift gate.

## Advanced supported surfaces

The following modules are supported for advanced integrations but are not part
of the compact `sose.api` facade:

- `sose.core.runtime` durable record definitions;
- `sose.core.resources`, `stores`, `containers`, and `preemption` managers;
- `sose.core.durable` scheduler/recovery participants;
- `sose.factories` and `sose.statecharts` integration helpers;
- detailed probability graph/evaluation types;
- persistence adapter implementation modules.

Changes here should remain deliberate and tested, but may occur more frequently
before 1.0 than changes to `sose.api`.

## Compatibility-only surfaces

`SimulationContext.scheduler` and direct use of the in-memory scheduler queue
remain available for older code. New domain code must schedule through
`context.schedules` so ScheduledWork is durable.

Compatibility-only surfaces may be deprecated and removed in a documented
future minor release.

## Internal

Private names (leading underscore) and implementation details not documented as
public or advanced have no compatibility promise.

Examples under `sose.examples` are executable specifications and architectural
evidence. They are not a generic reusable business-domain API.

## Optional backends

Concrete optional backend implementations are not imported by `sose.api`.
This keeps the stable facade importable without optional execution dependencies.

Integrators may explicitly import, for example:

```python
from sose.backends.simpy import SimPyBackend
```

after installing `sose[simpy]`.

## Promotion rule

A lower-level symbol should move into `sose.api` only when:

- multiple real consumers need it directly;
- its semantic contract is understood;
- its lifecycle/restart behavior is covered;
- maintaining compatibility is preferable to leaving it advanced.

The goal is a small dependable facade, not re-exporting the entire package.


## Runtime-construction completeness

`Scheduler` is public because `SimulationContext` requires it at construction
time. The v0.8 facade is expected to be sufficient to assemble the backend-neutral
runtime without importing `sose.core.*` implementation modules.

# Changelog

## 0.7.0 — 2026-09-26

### Added

- durable FIFO, priority, and filter Store semantics;
- durable Store get completion results and terminal request identity;
- durable Container definitions, levels, intents, and terminal operation results;
- crash-consistent durable preemption with terminal preemption records;
- full v0.7 multi-restart runtime equivalence gate;
- deterministic crash-injection test utilities and recovery matrix;
- reusable persistence conformance suite;
- named runtime recovery participants and explicit recovery ordering;
- CI matrix for Python 3.12, 3.13, and 3.14 with an 80% branch-coverage gate;
- formal cross-domain implementation blueprints.

### Changed

- Runtime recovery validates the complete durable plan before reconstruction.
- Durable Container replay is ordered explicitly by semantic sequence.
- Container quantities reject non-finite values.
- Runtime reconstruction is coordinated through named participants instead of
  subsystem-specific RuntimeRebuilder arguments.
- README and durable-runtime architecture documentation now describe the v0.7
  boundary.

### Compatibility

- `context.schedules` is the durable scheduling API.
- `context.scheduler` remains a legacy in-memory compatibility bridge and
  should not be used by new domain examples.
- SimPy-native environments, processes, events, queues, callbacks, requests,
  and generators remain intentionally non-durable.

### Architectural boundary

```text
durable semantic truth
    ≠
backend-native execution state
```

The next milestone is domain implementation, beginning with operational examples
that exercise the complete durable runtime.

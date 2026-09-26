# Runtime recovery phases — v0.7

Runtime recovery is a two-phase protocol:

```text
validate every participant
        ↓
reconstruct every participant
```

No reconstruction may begin until the complete durable recovery plan is valid.

## RecoveryParticipant

Each durable subsystem is registered as a named participant:

```python
RecoveryParticipant("resources", engine.resources)
RecoveryParticipant("stores", engine.stores)
RecoveryParticipant("preemptive-resources", engine.preemptive_resources)
RecoveryParticipant("containers", engine.containers)
```

A participant may expose:

- `validate_rebuild()` — optional, side-effect-free validation;
- `rebuild_backend(backend)` — required reconstruction operation.

The rebuilder no longer has subsystem-specific constructor arguments. Adding a
future durable primitive changes the Engine participant list rather than the
`RuntimeRebuilder` API.

## Stable Engine order

The v0.7 Engine declares:

```text
context
↓
resources
↓
stores
↓
preemptive-resources
↓
containers
↓
scheduled-work
```

Validation for every participant occurs before this reconstruction sequence.

Scheduled work is enqueued last so subsystem reconstruction cannot accidentally
execute future domain work during recovery.

## Architectural benefit

This separates **what must be recovered** from **how recovery is coordinated**
and keeps the coordinator closed to subsystem-specific branching.

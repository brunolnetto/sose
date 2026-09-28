# Runtime diagnostics

SOSE diagnostics are a read-only view over durable semantic truth.

They are deliberately **not** a logging framework and do not inspect
backend-native continuation state.

## API

```python
diagnostics = engine.diagnostics()

print(diagnostics.healthy)
print(diagnostics.position)
print(diagnostics.counts.scheduled_work)

for issue in diagnostics.issues:
    print(issue.code, issue.message)
```

The same collector is available directly:

```python
from sose.api import collect_runtime_diagnostics

diagnostics = collect_runtime_diagnostics(persistence)
```

## Snapshot contents

The snapshot includes:

- durable recovery position;
- event count;
- ScheduledWork count;
- scenario decisions and active activations;
- normal Resource demands, reservations, and release intents;
- Store items, pending puts/gets, and terminal get results;
- Container pending/completed operations;
- preemptive Resource demands, reservations, release intents, and preemption
  results.

These counts are semantic state counts, not backend queue/process metrics.

## Consistency warnings

The collector reports stable issue codes for durable references that cannot be
resolved, including:

- `scheduled.command_missing`;
- `resource.definition_missing`;
- `store.definition_missing`;
- `container.definition_missing`;
- `preemptive.definition_missing`.

Diagnostics never repair state. A caller may inspect problems before attempting
`Engine.rebuild_backend(...)`, exporting support information, or deciding on a
migration/recovery action.

## Observability boundary

The initial contract is intentionally pull-based and deterministic:

```text
Persistence
   |
   v
collect_runtime_diagnostics()
   |
   +-- counts
   +-- recovery position
   +-- consistency issues
```

A future logging, metrics, or OpenTelemetry integration should adapt this
semantic surface rather than making the core runtime depend on a telemetry SDK.

This preserves the architecture rule:

> durable truth is authoritative; execution/telemetry infrastructure is an
> observer.

## Stability

`RuntimeDiagnostics`, `RuntimeCounts`, `DiagnosticIssue`, and
`collect_runtime_diagnostics` are exported from `sose.api` and therefore
participate in the public compatibility contract.

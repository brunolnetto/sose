# Structural copy-on-write UnitOfWork experiment

## Question

The dirty-record work removed whole-state serialization/diffing from
record-oriented sinks, but every transaction still entered through:

```text
deepcopy(_State)
```

That copies the complete durable object graph even when one record changes.

This experiment asks whether a safer structural copy-on-write boundary can
remove that cost without changing Persistence semantics.

## Design

`fork_state(state)` creates a new `_State` where:

- every top-level dict/list collection is copied;
- existing durable record objects are shared;
- scalar values are shared;
- UnitOfWork getters continue to return deep copies;
- UnitOfWork save methods continue to deep-copy values before replacing records.

Therefore untouched durable records remain structurally shared, while any
record a caller can mutate through the public UnitOfWork API is still isolated.

Record-oriented sinks also keep the authoritative pre-transaction state by
reference rather than creating a second structural fork.

## Expected complexity

The experiment changes transaction entry from:

```text
O(total durable object graph)
```

deep copy to approximately:

```text
O(number of top-level collection entries)
```

for collection shell copying.

This is not yet true lazy record loading. Large dictionaries are still copied,
so scaling may remain linear with durable cardinality.

That is intentional: this PR tests the smallest safe copy-on-write step before
introducing overlay mappings or a new UnitOfWork implementation.

## Safety invariants

Executable tests prove:

- top-level mutable collections are isolated;
- untouched record objects may remain shared;
- `get_*` values remain detached copies;
- rollback cannot leak a saved mutation;
- committing one record does not replace untouched record objects.

All existing persistence conformance, restart, concurrency, and Reference tests
must remain unchanged.

## Success criterion

The existing persistence scaling benchmark at N=10/100/1,000/10,000 is the
decision instrument.

Keep this design if it materially lowers single-record update cost, especially
at larger cardinalities, without weakening semantic tests.

If the curve still grows strongly with N, the next justified experiment is a
true overlay/lazy-record UnitOfWork that avoids copying top-level collections
that are not touched.

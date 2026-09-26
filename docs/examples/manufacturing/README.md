# Manufacturing reference domain

This reference domain validates SOSE against a capacity-constrained production flow.

## Canonical flow

```text
ProductionOrder(planned)
  -> released
  -> setup
  -> producing
  -> inspection
  -> completed
```

Operational effects are explicit:

```text
raw material available
  -> machine + operator acquired
  -> material issued
  -> processing
  -> WIP produced
  -> inspection
  -> finished goods committed
```

Exception paths introduced incrementally:

- waiting for material;
- machine contention;
- machine breakdown;
- emergency maintenance preemption;
- quality hold / rework;
- downtime and yield scenarios;
- interrupted execution and restart.

## Reference-domain gates

Manufacturing is promoted to a reference implementation only when:

1. lifecycle state is explicit and durable;
2. machine/operator possession gates production transitions;
3. material consumption is durable before production claims begin;
4. WIP and finished-goods effects are durable before completion claims;
5. breakdown and recovery are represented as business state, not backend state;
6. preemption outcomes are durable;
7. scenarios expire without implicit retriggering;
8. continuous and multi-restart execution are semantically equivalent.

The core invariant remains:

```text
domain semantics are durable
backend execution state is ephemeral and reconstructible
```


## Implemented reference surface

The executable reference now covers:

- durable ProductionOrder and Operation lifecycles;
- machine and operator contention;
- explicit material shortage and resumption;
- raw-material issue and WIP transfer;
- durable machine breakdown and preemption;
- repair and resource reacquisition;
- finite scenario interventions;
- deterministic multi-restart equivalence.

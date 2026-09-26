# Manufacturing flow

## Happy path

```text
ProductionOrder(planned)
    |
    | release
    v
ProductionOrder(released)
    |
    | acquire machine + operator
    v
ProductionOrder(setup)
    |
    | durable raw-material issue
    v
ProductionOrder(producing)
    |
    | durable WIP/output
    v
ProductionOrder(inspection)
    |
    | durable finished-goods commit
    v
ProductionOrder(completed)
```

## Recovery boundaries

Restart equivalence must hold:

- after release while resources are queued;
- after machine acquisition but before operator acquisition;
- after material issue intent/result but before production transition;
- while production is displaced by a breakdown;
- after WIP output but before inspection/completion;
- after finished-goods commit but before the final lifecycle transition.

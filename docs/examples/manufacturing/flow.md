# Manufacturing flow

## Happy path

```mermaid
flowchart TD
    A["ProductionOrder(planned)"] -->|release| B["ProductionOrder(released)"]
    B -->|acquire machine + operator| C["ProductionOrder(setup)"]
    C -->|durable raw-material issue| D["ProductionOrder(producing)"]
    D -->|durable WIP/output| E["ProductionOrder(inspection)"]
    E -->|durable finished-goods commit| F["ProductionOrder(completed)"]
```

## Recovery boundaries

Restart equivalence must hold:

- after release while resources are queued;
- after machine acquisition but before operator acquisition;
- after material issue intent/result but before production transition;
- while production is displaced by a breakdown;
- after WIP output but before inspection/completion;
- after finished-goods commit but before the final lifecycle transition.

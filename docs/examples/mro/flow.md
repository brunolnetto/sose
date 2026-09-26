# Maintenance / MRO flow

## Happy path

```mermaid
flowchart TD
    A["WorkOrder(planned)"] -->|release| B["WorkOrder(released)"]
    B --> C{"Spare part available?"}
    C -->|yes| D["Acquire technician"]
    D --> E["Acquire maintenance bay"]
    E --> F["Consume Store lot + Container quantity"]
    F --> G["WorkOrder(in_progress)"]
    G -->|complete| H["WorkOrder(completed)"]
    H -->|close| I["WorkOrder(closed)"]
    I --> J["Release capacity"]
```

## Material-shortage path

```mermaid
flowchart TD
    A["WorkOrder(released)"] --> B{"Part available?"}
    B -->|no| C["WorkOrder(waiting_material)"]
    C --> D["PartDemand(waiting_inventory)"]
    D --> E["Replenishment / availability restored"]
    E --> F["material_ready"]
    F --> A
```

## Resource-contention path

```mermaid
flowchart TD
    A["WorkOrder(released)"] --> B["Request technician + bay"]
    B --> C{"Both reservations acquired?"}
    C -->|no| D["WorkOrder(waiting_resource)"]
    D --> E["Capacity becomes available"]
    E --> F["resource_ready"]
    F --> A
```

## Emergency / preemption path

```mermaid
flowchart TD
    A["WorkOrder(in_progress)"] --> B["Emergency bay request"]
    B --> C["ResourcePreemptionResult"]
    C --> D["WorkOrder(interrupted)"]
    D --> E["Emergency releases bay"]
    E --> F["Normal work reacquires bay"]
    F --> G["resume"]
    G --> A
```

## Recovery boundaries

Restart equivalence is verified across:

- resource queues;
- completed part issue before `start`;
- active maintenance;
- material shortage;
- emergency interruption;
- completion before closure;
- cancellation;
- scenario-owned emergency cleanup.

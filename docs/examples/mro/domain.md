# Maintenance / MRO domain model

## Entities

### WorkOrder

Represents the durable maintenance lifecycle for the reference work order.

```mermaid
stateDiagram-v2
    [*] --> planned
    planned --> released: release
    planned --> cancelled: cancel
    released --> waiting_material: wait_for_material
    waiting_material --> released: material_ready
    released --> waiting_resource: wait_for_resource
    waiting_resource --> released: resource_ready
    waiting_resource --> waiting_material: wait_for_material
    released --> in_progress: start
    in_progress --> interrupted: interrupt
    interrupted --> in_progress: resume
    in_progress --> completed: complete
    completed --> closed: close
    released --> cancelled: cancel
    waiting_material --> cancelled: cancel
    waiting_resource --> cancelled: cancel
```

### PartDemand

Represents the spare-part requirement associated with the maintenance flow.

```mermaid
stateDiagram-v2
    [*] --> open
    open --> waiting_inventory: wait
    open --> allocated: allocate
    waiting_inventory --> allocated: allocate
    allocated --> consumed: consume
    open --> cancelled: cancel
    waiting_inventory --> cancelled: cancel
```

## Commands

Work-order commands:

- `release`
- `wait_for_material`
- `material_ready`
- `wait_for_resource`
- `resource_ready`
- `start`
- `interrupt`
- `resume`
- `complete`
- `close`
- `cancel`

Part-demand commands:

- `wait`
- `allocate`
- `consume`
- `cancel`

## Invariants

1. active maintenance requires durable technician and maintenance-bay reservations;
2. spare-part lot and quantity withdrawals must be terminal before `start`;
3. a work order cannot retain constrained capacity while waiting for missing material;
4. emergency displacement must be durable and business-visible;
5. scenario-owned emergency capacity must be released on expiry;
6. cancellation is allowed only before spare-part issue starts;
7. restart must not duplicate part consumption, capacity acquisition, preemption, or lifecycle claims.

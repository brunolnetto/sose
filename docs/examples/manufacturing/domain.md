# Manufacturing domain model

## Entities

### ProductionOrder

Lifecycle:

```mermaid
stateDiagram-v2
    [*] --> planned
    planned --> released: release
    released --> setup: begin_setup
    setup --> producing: start_production
    producing --> inspection: begin_inspection
    inspection --> completed: complete
    released --> waiting_material: wait_for_material
    waiting_material --> released: material_ready
    setup --> machine_down: breakdown
    producing --> machine_down: breakdown
    machine_down --> setup: repair
    inspection --> quality_hold: hold_quality
    quality_hold --> rework: rework_order
    rework --> producing: resume_rework
```

### Operation

Represents the executable routing step associated with the production order.

Lifecycle:

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> ready_state: ready
    ready_state --> running: start
    running --> done: finish
    ready_state --> blocked: block
    running --> blocked: block
    blocked --> ready_state: unblock
    done --> ready_state: rework
```

## Commands

Production order commands:

- `release`
- `begin_setup`
- `start_production`
- `begin_inspection`
- `complete`
- `wait_for_material`
- `material_ready`
- `breakdown`
- `repair`
- `hold_quality`
- `rework`

Operation commands:

- `ready`
- `start`
- `finish`
- `block`
- `unblock`

## Invariants

1. setup requires a durable machine reservation and operator reservation;
2. production cannot start until material issue is durably complete;
3. completion cannot precede durable finished-goods output;
4. a machine breakdown must be visible in durable business state;
5. preemption must not erase the displaced production identity;
6. restart must not duplicate material issue, WIP, finished goods, or quality outcomes.

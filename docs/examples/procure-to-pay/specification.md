# Procure-to-Pay Operational Reference Specification

## 1. Purpose and scope

This example models the operational procurement slice that begins with an internal
material request and ends when received inventory is consumed.

It demonstrates how SOSE represents procurement lifecycles, supplier lead time,
receiving capacity, inventory, shortage/backorder behavior, receipt exceptions,
scenario interventions, and restart-safe physical effects.

The example intentionally does **not** claim to implement the full financial
Procure-to-Pay process.

### In scope

- internal requisition;
- purchase order;
- durable supplier lead time;
- physical receipt;
- receiving dock and inspection capacity;
- accepted, partial, and rejected receipts;
- inventory lot identity and quantitative balance;
- material demand;
- shortage and backorder;
- supplier/demand/receiving scenarios;
- continuous-versus-restarted semantic equivalence.

### Outside the executable slice

- supplier invoice;
- three-way match;
- invoice hold;
- accounts payable;
- payment authorization/execution;
- financial settlement.

These remain part of the broader P2P blueprint but are not part of this executable
operational reference.

## 2. Operational story

A material need creates an internal requisition. The requisition is approved and
converted into an ordered state.

A purchase order is submitted to a supplier, confirmed, dispatched, and then waits
through a durable supplier lead time before it can be received.

Physical receipt cannot advance independently of receiving capacity. A receipt must
obtain the receiving dock and inspector resources before it may claim the corresponding
business transitions.

An accepted receipt is stocked only after the discrete received lot and the quantitative
inventory increase are durable.

Material demand consumes inventory only after both the Store lot withdrawal and the
Container quantity withdrawal complete.

If inventory is insufficient, the demand becomes explicitly waiting/backordered before
withdrawal is attempted.

A partial receipt stocks only the quantity actually received; residual demand remains
unsatisfied. A rejected receipt terminates without creating inventory.

## 3. Domain entities

### 3.1 Requisition

**Responsibility**

Represents internal authorization to procure material.

**Relevant attributes**

- `sku`;
- `quantity`.

**Owns**

- requested/approved/ordered/rejected lifecycle.

**Does not own**

- supplier commitment;
- physical receipt;
- inventory;
- material consumption.

### 3.2 PurchaseOrder

**Responsibility**

Represents the supplier-facing procurement commitment and supplier-delivery lifecycle.

**Relevant attributes**

- `sku`;
- `quantity`;
- `supplier`.

**Owns**

- submission;
- supplier confirmation;
- transit/delay;
- receipt/closure/cancellation state.

**Does not own**

- receiving capacity;
- inspection result;
- inventory quantity;
- downstream material-demand state.

### 3.3 Receipt

**Responsibility**

Represents the physical arrival and receiving/inspection lifecycle.

**Relevant attributes**

- `sku`;
- `quantity`.

**Owns**

- pending/receiving/partial/inspected/stocked/rejected lifecycle.

**Does not own**

- dock or inspector availability;
- inventory balance;
- material-demand allocation.

### 3.4 MaterialDemand

**Responsibility**

Represents a need to consume procured material.

**Relevant attributes**

- `sku`;
- `quantity`.

**Owns**

- open;
- waiting inventory;
- backorder;
- allocation;
- consumption/cancellation.

**Does not own**

- physical inventory truth;
- supplier state;
- receipt state.

## 4. StateCharts

### 4.1 Requisition StateChart

```text
requested
  ├──approve──> approved ──order──> ordered
  └──reject──────────────────────> rejected
```

| Current state | Command | Preconditions | Next state |
|---|---|---|---|
| `requested` | `approve` | requisition accepted | `approved` |
| `requested` | `reject` | requisition denied | `rejected` |
| `approved` | `order` | supplier order may be created | `ordered` |

### 4.2 PurchaseOrder StateChart

```text
created
  │ submit
  ▼
submitted
  │ confirm
  ▼
confirmed ──mark_delayed──> delayed
  │ dispatch                  │ dispatch
  ▼                           ▼
in_transit ─mark_delayed──> delayed
  │ receive
  ▼
received
  │ close
  ▼
closed
```

Cancellation is legal from `created`, `submitted`, and `confirmed`.

| Current state | Command | Preconditions | Next state | Durable evidence |
|---|---|---|---|---|
| `created` | `submit` | PO created | `submitted` | command/event |
| `submitted` | `confirm` | supplier confirms | `confirmed` | command/event |
| `confirmed` / `delayed` | `dispatch` | shipment dispatched | `in_transit` | command/event |
| `confirmed` / `in_transit` | `mark_delayed` | supplier delay observed | `delayed` | command/event |
| `in_transit` | `receive` | durable lead-time work reaches delivery | `received` | ScheduledWork execution |
| `received` | `close` | PO receiving obligation satisfied | `closed` | command/event |

### 4.3 Receipt StateChart

```text
pending
  │ begin_receiving
  ▼
receiving ──mark_partial──> partial
  │  │                     │
  │  ├──inspect────────────┤
  │  │                     ▼
  │  └──reject────────> rejected
  ▼
inspected
  ├──stock──> stocked
  └──reject─> rejected
```

| Current state | Command | Operational precondition | Next state | Durable evidence |
|---|---|---|---|---|
| `pending` | `begin_receiving` | receiving dock held | `receiving` | ResourceReservation |
| `receiving` | `mark_partial` | delivered quantity is partial | `partial` | transition event |
| `receiving` / `partial` | `inspect` | inspector held | `inspected` | ResourceReservation |
| `receiving` / `partial` / `inspected` | `reject` | receipt rejected | `rejected` | transition event |
| `inspected` | `stock` | lot + quantity effects durable | `stocked` | Store + Container results |

### 4.4 MaterialDemand StateChart

```text
open
  ├──wait_for_inventory──> waiting_inventory
  │                            │
  ├──backorder─────────────────┤
  │                            ▼
  │                        backordered
  │                            │
  └────────allocate────────────┘
               │
               ▼
           allocated
               │ consume
               ▼
           consumed
```

Cancellation is legal from `open`, `waiting_inventory`, and `backordered`.

| Current state | Command | Operational precondition | Next state | Durable evidence |
|---|---|---|---|---|
| `open` | `wait_for_inventory` | insufficient stock | `waiting_inventory` | inventory state |
| `open` / `waiting_inventory` | `backorder` | shortage remains unresolved | `backordered` | transition event |
| `open` / `waiting_inventory` / `backordered` | `allocate` | Store + Container withdrawals terminal | `allocated` | terminal inventory results |
| `allocated` | `consume` | allocation completed | `consumed` | transition event |

## 5. Process specifications

### 5.1 Happy path

```text
Requisition(requested)
    │ approve / order
    ▼
Requisition(ordered)
    │
    ▼
PurchaseOrder(created)
    │ submit / confirm / dispatch
    ▼
PurchaseOrder(in_transit)
    │ durable supplier lead time
    ▼
PurchaseOrder(received)
    │
    ▼
Receipt(pending)
    │ acquire receiving dock
    ▼
Receipt(receiving)
    │ acquire inspector
    ▼
Receipt(inspected)
    │ commit Store lot + Container quantity
    ▼
Receipt(stocked)
    │
    ▼
MaterialDemand
    │ withdraw Store lot + Container quantity
    ▼
allocated
    │ consume
    ▼
consumed
```

The receipt is not considered stocked before both inventory representations are
durable. Material is not considered allocated before both withdrawals are terminal.

### 5.2 Sad path — receiving contention

**Trigger**

Another receipt holds the receiving dock or inspector.

**Expected behavior**

The new receipt remains queued and cannot advance into the lifecycle state that claims
the unavailable capacity.

**Durable truth**

Resource demand/reservation state survives restart.

**Recovery**

When capacity is released, the pending request is promoted and the lifecycle may advance.

### 5.3 Sad path — shortage and backorder

**Trigger**

Required inventory is unavailable.

**Expected behavior**

```text
MaterialDemand(open)
  │
  ▼
waiting_inventory
  │
  ▼
backordered
  │ replenishment
  ▼
allocated
  │
  ▼
consumed
```

**Durable truth**

The demand state exposes the shortage. Pending operational work cannot disappear across
restart.

**Recovery**

Replenishment makes the inventory operations satisfiable exactly once.

### 5.4 Sad path — partial receipt

**Trigger**

Supplier delivers less than the demanded quantity.

**Expected behavior**

```text
receiving
  │ mark_partial
  ▼
partial
  │ inspect
  ▼
inspected
  │ stock received quantity only
  ▼
stocked

available quantity < required demand
  ↓
MaterialDemand(backordered)
```

**Durable truth**

The partial lot and received quantity remain intact. Residual demand is explicit.

**Critical rule**

Shortage is detected before withdrawal. A partial Store lot must not be consumed while
the corresponding Container withdrawal remains blocked.

### 5.5 Sad path — rejected receipt

**Trigger**

Receiving/inspection rejects the shipment.

**Expected behavior**

```text
receiving / partial / inspected
  │ reject
  ▼
rejected
```

**Durable truth**

- receipt remains terminally rejected;
- no Store inventory item is created;
- no Container inventory quantity is added;
- held resources are released.

### 5.6 External disruptions — scenarios

Representative finite scenarios include:

- supplier delay / lead-time inflation;
- demand spike;
- receiving congestion.

Scenarios affect operational context. They do not bypass lifecycle or inventory
invariants.

## 6. Commands and domain events

| Command | Target | Meaning |
|---|---|---|
| `approve`, `reject`, `order` | Requisition | internal procurement authorization |
| `submit`, `confirm`, `dispatch`, `mark_delayed`, `receive`, `close` | PurchaseOrder | supplier-delivery lifecycle |
| `begin_receiving`, `mark_partial`, `inspect`, `reject`, `stock` | Receipt | physical receipt lifecycle |
| `wait_for_inventory`, `backorder`, `allocate`, `consume`, `cancel` | MaterialDemand | inventory-demand lifecycle |

Successful transitions emit immutable `entity.state_transition` events.

The end-to-end flow uses one stable correlation identity so requisition, PO, receipt,
stocking, and material consumption can be reconstructed as one causal process.

## 7. Invariants

**P2P-01 — Requisition authorization**

A requisition cannot become `ordered` before it becomes `approved`.

**P2P-02 — Supplier lifecycle**

A purchase order cannot become `received` before confirmation, dispatch, and durable
supplier lead-time execution.

**P2P-03 — Receiving capacity**

A receipt cannot claim `receiving` or inspection progress without the required durable
resource reservation.

**P2P-04 — Stocking evidence**

A receipt cannot become `stocked` before both its discrete Store lot and quantitative
Container effect are durable.

**P2P-05 — Allocation evidence**

A material demand cannot become `allocated` before both Store and Container withdrawal
operations are terminal.

**P2P-06 — No split inventory withdrawal**

When Store and Container jointly represent one inventory position, feasibility must be
established before a shortage path can partially execute only one representation.

**P2P-07 — Partial receipt preserves residual shortage**

A partial receipt may stock only the delivered quantity; it must not imply that the full
material demand is satisfied.

**P2P-08 — Rejection is inventory-neutral**

A rejected receipt must create no durable inventory effect.

**P2P-09 — Terminal request identity**

Completed physical operations cannot apply the same effect twice after retry/restart.

**P2P-10 — Correlated business trace**

The operational flow uses stable correlation so related transitions remain one trace.

**P2P-11 — Restart equivalence**

Continuous and restarted executions must produce equivalent semantic outcomes across
representative happy and sad paths.

## 8. Durable truth and ownership

| Concept | Durable owner | Why |
|---|---|---|
| requisition lifecycle | `Requisition.state` | internal authorization truth |
| supplier commitment/delivery | `PurchaseOrder.state` | supplier lifecycle truth |
| physical receiving lifecycle | `Receipt.state` | arrival/inspection truth |
| demand lifecycle | `MaterialDemand.state` | shortage/allocation/consumption truth |
| received lot identity | Store `received_lots` | discrete inventory identity |
| inventory quantity | Container `inventory` | quantitative stock truth |
| receiving capacity | Resource demand/reservation | constrained capacity |
| supplier timing | ScheduledWork | durable lead-time intent |
| scenario activation | ScenarioRuntimeState | intervention truth |
| recovery point | SimulationPosition | logical reconstruction position |

Backend-native SimPy environments, requests, events, callbacks, queues, and generators
are ephemeral and reconstructible.

## 9. Restart semantics

Meaningful crash boundaries include:

1. while supplier lead-time work remains scheduled;
2. while a receipt is queued for receiving capacity;
3. after one inventory intent/result but before the paired effect is complete;
4. while demand is waiting/backordered;
5. after a partial-receipt transition but before stocking;
6. after rejection;
7. after scenario activation.

Semantic equivalence means the same durable entity states, inventory quantities and
items, terminal request results, resource truth, scenario state, and causal event history.

## 10. Scenario specification

### Supplier delay

- one-shot finite activation;
- sets supplier delay context and lead-time multiplier;
- does not directly assign PurchaseOrder state.

### Demand spike

- one-shot finite activation;
- increases demand multiplier;
- may create shortage pressure.

### Receiving congestion

- one-shot finite activation;
- reduces effective receiving capacity context;
- contention remains governed by resource semantics.

## 11. Example runs

### A. Nominal

```text
request
→ requisition approved/ordered
→ PO submitted/confirmed/dispatched
→ lead time
→ receipt
→ dock + inspector
→ inventory stocked
→ material allocated/consumed
```

### B. Receiving contention

```text
receipt A holds dock
receipt B requests dock
→ B remains pending
→ A releases
→ B acquires
→ B enters receiving
```

### C. Partial receipt

```text
required = 5
received = 3
→ receipt partial/inspected/stocked(3)
→ inventory remains 3
→ demand becomes backordered
→ partial lot is not prematurely consumed
```

### D. Rejected receipt

```text
receipt arrives
→ receiving
→ reject
→ rejected
→ inventory remains 0
```

## 12. Executable evidence

| Specification area | Implementation | Tests |
|---|---|---|
| entity StateCharts | `entities.py`, `statecharts.py` | happy-path/state tests |
| happy path | `run_happy_path`, reconcilers | `test_p2p_happy_path.py` |
| receiving contention | receiving-resource reconciler | `test_p2p_receiving_resources.py` |
| shortage/backorder | shortage flow/reconcilers | `test_p2p_shortage_backorder.py` |
| scenarios | `scenarios.py` | `test_p2p_scenarios.py` |
| happy-path restart | runtime/reconcilers | `test_p2p_restart_equivalence.py` |
| partial/rejected receipts | receiving outcome handling | `test_p2p_receipt_exceptions.py` |
| receipt sad-path restart | same | `test_p2p_receipt_exception_restart.py` |

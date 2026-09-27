# Logistics & Transport Reference Domain Specification

## 1. Purpose and scope

This example models a shipment moving through pickup, origin-hub handling, line-haul
transfer, destination-hub handling, and last-mile delivery.

The domain is intentionally different from the existing inventory-centric examples:
its core pressure is physical-flow queueing, constrained transport capacity, scheduled
commitments, delivery failure/retry, and durable reconstruction.

Initial executable scope:

- Shipment and DeliveryAttempt durable entities;
- pickup scheduling lifecycle;
- origin and destination hub progression;
- transfer and last-mile lifecycle;
- explicit delay/resume;
- failed delivery and retry lifecycle;
- lost, damaged, and returned terminal outcomes;
- congestion, courier-capacity loss, and weather-delay scenarios.

Out of scope for this slice: route optimization, geospatial routing, customs, carrier
pricing, fleet maintenance, and multi-package consolidation.

## 2. Operational story

A Shipment starts in created. Pickup must be scheduled before collection. After pickup,
the shipment reaches the origin hub, transfers to the destination hub, and enters
last-mile delivery.

A DeliveryAttempt is a separate durable occurrence. Dispatch does not imply success:
the attempt may be delivered or fail. Failed attempts may be durably scheduled for
retry or exhausted.

Delay is explicit business state. Resume is deliberately contextual: orchestration
chooses whether the delayed shipment resumes pickup, transfer, or delivery. Lost,
damaged, and returned are terminal business outcomes and are never inferred merely
from disappearance from an ephemeral backend queue.

## 3. Domain entities

### 3.1 Shipment

Owns the end-to-end logistics lifecycle.

States: created, pickup_scheduled, picked_up, at_origin_hub, in_transfer,
at_destination_hub, out_for_delivery, phase-specific delayed states, delivered, lost,
damaged, returned.

Delay phase is durable in the StateChart itself: each resumable source state maps to a
distinct delayed state and `resume` returns only to that exact source phase.

It does not own backend queue objects, courier handles, dock request objects, callbacks,
or generator continuation state.

### 3.2 DeliveryAttempt

Represents one last-mile delivery occurrence rather than a mutable counter hidden in
Shipment.

States: pending, out_for_delivery, delivered, failed.

`failed` is terminal for that occurrence. A retry is not a transition that reuses the
failed entity; orchestration schedules creation of a new deterministic DeliveryAttempt
identity under the same shipment correlation.

## 4. Persistent data model / ERD

The relationships here are semantic, not physical SQL foreign-key declarations.

### 4.1 Business entities ERD

    Shipment 1 ---- 0..N DeliveryAttempt
             process correlation

The relationship is process-level correlation. The current Entity base model does not
require a persisted object reference from one entity to the other.

### 4.2 Durable operational ERD

Target durable relationships:

    Shipment / DeliveryAttempt
        -> Command -> ScheduledWork
        -> DomainEvent

    ResourceDefinition
        -> ResourceDemand -> ResourceReservation -> ResourceReleaseIntent

    StoreDefinition
        -> DurableStoreItem
        -> StorePutIntent
        -> StoreGetRequest -> StoreGetResult

    ScenarioRuntimeState
    SimulationPosition

Planned named objects for the full executable slice:

- Resources: pickup_courier, origin_dock, transfer_vehicle, destination_dock, delivery_courier;
- Stores: origin_hub_queue, destination_hub_queue;
- Scheduled work: pickup window, transfer departure, SLA deadline, retry;
- Scenario state and logical recovery position.

SimulationPosition remains the singleton logical recovery boundary. It is not connected
to Shipment through an invented business relationship.

### 4.3 Persistence ownership

| Business fact | Durable owner |
|---|---|
| shipment lifecycle | Shipment.state |
| delivery-attempt lifecycle | DeliveryAttempt.state |
| future pickup / retry / SLA | Command + ScheduledWork |
| dock/courier/vehicle demand | ResourceDemand |
| held capacity | ResourceReservation |
| interrupted release | ResourceReleaseIntent |
| hub queue identity/order | Store durable records |
| lifecycle history | DomainEvent |
| scenario intervention | ScenarioRuntimeState |
| logical recovery boundary | SimulationPosition |

## 5. StateCharts

### 5.1 Shipment StateChart

Nominal path:

    created
    -> pickup_scheduled
    -> picked_up
    -> at_origin_hub
    -> in_transfer
    -> at_destination_hub
    -> out_for_delivery
    -> delivered

Representative exception topology:

- each resumable phase has its own delayed state;
- `resume` returns only to the exact pre-delay phase;
- picked-up/transit/delivery states may terminate as lost or damaged;
- out_for_delivery may terminate as returned.

### 5.2 DeliveryAttempt StateChart

    pending
    -> out_for_delivery
       -> delivered
       -> failed

`delivered` and `failed` are terminal for one physical attempt. Only deliver and fail are
direct stochastic outcomes. Retry scheduling happens outside the failed attempt's
StateChart and creates a new correlated DeliveryAttempt.

## 6. Process specifications

### 6.1 Happy path

    Shipment(created)
    -> schedule pickup durably
    -> acquire pickup courier
    -> picked_up
    -> durable origin-hub queue progression
    -> acquire transfer capacity
    -> in_transfer
    -> durable destination-hub queue progression
    -> create DeliveryAttempt
    -> acquire delivery courier
    -> attempt delivered
    -> Shipment(delivered)

Lifecycle claims must follow durable operational evidence.

### 6.2 Sad path — hub congestion

A shipment reaches a hub while service capacity is constrained. Queue membership/order
must remain durable, no backend queue identity is persisted, and restart rebuilds the
ephemeral queue from Store/resource truth.

### 6.3 Sad path — failed delivery and retry

    DeliveryAttempt#1(pending)
    -> dispatch
    -> out_for_delivery
    -> fail
    -> failed (terminal)
    -> schedule retry durably
    -> create DeliveryAttempt#2(pending)
    -> dispatch
    -> out_for_delivery
    -> deliver
    -> delivered

A retry preserves DeliveryAttempt#1 as immutable failed business evidence and creates a
new deterministic occurrence identity for DeliveryAttempt#2.

### 6.4 Sad path — capacity loss

Courier/dock capacity may become unavailable through scenario state. The scenario does
not mutate Shipment state directly; orchestration interprets changed context and chooses
a legal wait/delay path.

### 6.5 Sad path — lost / damaged / returned

These outcomes are explicit durable terminal transitions. They must be represented by
entity state plus immutable event history, never by absence from a queue.

## 7. Commands and domain events

Shipment command vocabulary includes schedule_pickup, pickup, arrive_origin_hub,
dispatch_transfer, arrive_destination_hub, dispatch_delivery, deliver, delay,
resume, mark_lost, mark_damaged, and return_to_sender. The phase-specific delayed
state determines where `resume` returns.

DeliveryAttempt transition commands include dispatch, deliver, and fail. `dispatch` is
legal but excluded from direct probabilistic selection so courier-capacity orchestration
must authorize it. Retry scheduling is orchestration that creates a new correlated
DeliveryAttempt rather than mutating a failed attempt.

Successful transitions should emit immutable state-transition events under a stable
shipment correlation identity.

## 8. Invariants

LOG-01 — Explicit pickup scheduling: Shipment cannot become picked up directly from created.

LOG-02 — Durable hub waiting: hub waiting must be reconstructible from durable Store/resource truth.

LOG-03 — Capacity before movement: constrained capacity must exist before the corresponding physical movement is claimed.

LOG-04 — Attempt identity: a failed delivery occurrence is terminal and remains durable;
a retry uses a distinct deterministic DeliveryAttempt identity.

LOG-05 — Outcome gating: Shipment becomes delivered only after a delivery attempt durably delivers.

LOG-06 — Scenario discipline: scenarios alter context; they do not assign Shipment state directly.

LOG-07 — Terminal exception truth: lost, damaged, and returned are explicit durable outcomes.

LOG-08 — Restart equivalence: continuous and reconstructed executions converge at pickup wait, hub queue, transfer wait, failed-attempt/retry, and final-delivery boundaries.

## 9. Durable truth and ownership

Durable truth consists of entities, commands, events, schedules, resource demand/ownership,
Store queue records, scenario state, and logical simulation position.

Ephemeral truth includes SimPy Environment/Process/Event/Request objects, callbacks,
resource handles, native queues, and Python generators.

## 10. Restart semantics

Reference-grade restart gates will cover:

1. pickup scheduled but not executed;
2. queued at origin hub;
3. transfer capacity pending;
4. queued at destination hub;
5. delivery courier pending;
6. failed terminal attempt with creation of the next retry attempt scheduled;
7. delivered attempt before Shipment finalization.

Backend-native object identity is excluded from semantic equivalence.

## 11. Scenario specification

Hub congestion reduces effective hub-service capacity. Courier capacity loss makes the
courier prerequisite unavailable. Weather delay increases transfer-delay context.
None of these scenarios assigns business lifecycle state directly.

## 12. Example runs

Nominal:

    created -> pickup_scheduled -> picked_up -> at_origin_hub
    -> in_transfer -> at_destination_hub -> out_for_delivery -> delivered

Failed attempt then retry:

    Shipment(out_for_delivery) + DeliveryAttempt#1(out_for_delivery)
    -> DeliveryAttempt#1(failed terminal)
    -> durable retry schedule
    -> DeliveryAttempt#2(pending)
    -> dispatch -> deliver -> Shipment(delivered)

## 13. Executable evidence

| Specification area | Current implementation/evidence | Status |
|---|---|---|
| Shipment entity | entities.py | implemented |
| DeliveryAttempt entity | entities.py | implemented |
| Shipment StateChart | statecharts.py + topology test | implemented |
| DeliveryAttempt StateChart | statecharts.py + topology test | implemented |
| stochastic delivery outcome | TransitionPolicy + test | implemented |
| logistics scenarios | scenarios.py | implemented |
| durable pickup scheduling | — | missing |
| hub Store queues | — | missing |
| constrained courier/dock/vehicle resources | — | missing |
| failed-attempt durable retry execution | — | missing |
| end-to-end happy path | — | missing |
| hub congestion sad path | — | missing |
| restart equivalence | — | missing |

## Promotion decision

Current status: **Partial**.

The lifecycle contract is executable, but the domain must not be promoted to Reference
implementation until queue/capacity mechanics, durable scheduling, end-to-end happy/sad
flows, and restart-equivalence evidence are implemented.

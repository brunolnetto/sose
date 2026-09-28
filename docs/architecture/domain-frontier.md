# Domain Frontier: What Should Challenge SOSE Next?

## Purpose

The Reference suite should not grow by replacing nouns in an existing workflow.

A new domain earns promotion work when it introduces **semantic pressure** that
the current References do not already demonstrate well enough.

The selection rule is:

> prefer domains that can falsify current architecture assumptions.

This document records the next domain frontier after the first fifteen
References, the Money audit, and the Telecommunications promotion. The remaining
frontier is ordered by the new semantic pressure each domain can place on the
architecture.

## Current coverage

The Reference suite already exercises:

- durable lifecycle/statechart semantics;
- resource contention and preemption;
- delayed/scheduled work;
- Store selection and durable selection ownership;
- crash/restart equivalence;
- immutable occurrences;
- illegal prerequisites;
- probabilistic transitions;
- long-running operations;
- fulfillment and supply-side flow;
- financial obligations and payment occurrences;
- aviation/airport capacity and disruption;
- healthcare priority and transfer;
- construction prerequisites;
- assurance-style incident recovery.

The Telecommunications Reference adds another important dimension:

- commercial ProductOrder distinct from technical ServiceOrder;
- long-lived service inventory distinct from fulfillment orders;
- asynchronous activation eligibility;
- immutable non-rated usage;
- network Alarm distinct from TroubleTicket;
- service suspension/restoration.

## Selection principles

### 1. Add semantics, not sectors

A domain with familiar entities but no new invariants is weak evidence.

Example: another ordinary purchase/order workflow would add less value than a
metering domain where measurements are immutable occurrences whose late arrival,
correction, aggregation, and outage context must survive restart.

### 2. Prefer established domain vocabulary

Reference specifications should be grounded in real standards where practical.
This reduces accidental invention of unrealistic entity boundaries.

### 3. Separate domain meaning from runtime mechanics

The domain specification should explain *why* work is legal. Core APIs should
own *how* durable mechanics survive retries, races, and rebuild.

### 4. Stop each domain at the first coherent Reference slice

A Reference is not an industry simulator. Deeper slices should be added only
when they introduce new semantic pressure.

---

## Frontier 1 — Energy / Utilities

### Why it is high value

Energy introduces a different kind of durable truth from orders or tickets:
**measurements and grid state**.

Useful grounding includes:

- IEC Common Information Model families used across utility information models;
- OpenADR for demand-response/event interaction.

OpenADR 3 defines a standardized information/API model for demand-response
functionality, making it a useful source for event, target, program, and report
boundaries without requiring vendor-specific grid commands.

### Candidate entities

- Meter;
- MeterReading;
- ServicePoint;
- Outage;
- RestorationWork;
- DemandResponseEvent;
- Enrollment / Participation;
- ConsumptionInterval.

### New semantic pressure

- immutable timestamped measurement occurrences;
- late-arriving measurements;
- corrections without rewriting prior evidence;
- interval aggregation;
- outage impact over many service points;
- restoration causality;
- capacity/demand constraints;
- demand-response opt-in/opt-out and event windows;
- scheduled eligibility with target populations rather than one target entity.

### What would count as a strong first slice

A ServicePoint with immutable interval MeterReading occurrences **and explicit
correction lineage**, overlapping outage ownership/restoration, and a
demand-response event whose Participation explicitly targets that ServicePoint
inside a finite event window.

That is the minimum useful slice: ordinary readings plus one timer are not enough
to justify Energy as a new Reference because those mechanics are already covered
elsewhere.

### What should remain out of scope initially

- tariff billing;
- settlement markets;
- complex power-flow simulation;
- multi-currency energy billing.

Those would reopen Money or numerical-modeling questions only when evidence
requires it.

---

## Frontier 2 — Public Transit / Rail

### Why it is high value

GTFS and GTFS Realtime provide unusually concrete semantics for scheduled trips
and realtime deviations.

GTFS Realtime distinguishes:

- Trip Updates;
- Service Alerts;
- Vehicle Positions.

Its best practices also emphasize persistent identifiers across feed iterations.

### Candidate entities

- ScheduledTrip;
- VehicleRun;
- Vehicle;
- StopCall;
- TripUpdate;
- ServiceAlert;
- VehiclePositionOccurrence.

### New semantic pressure

- planned schedule versus realtime operational truth;
- repeated updates to the same trip identity;
- immutable position observations versus current projected state;
- delay propagation along ordered stop sequences;
- cancellation, detour, and added-trip semantics;
- freshness/staleness rules;
- one physical vehicle serving multiple trips over time.

### Strong first slice

One vehicle block serving two trips with delayed first-leg arrival propagating
into the second trip, plus immutable position observations and a ServiceAlert
covering a disruption interval.

### Architectural question it can falsify

Whether SOSE cleanly separates:

- durable occurrence history;
- current operational projection;
- future schedule;
- externally published realtime view.

---

## Frontier 3 — Field Service / Workforce

### Why it is high value

TM Forum Appointment Management treats an appointment as an arrangement for
provider personnel to perform work at a specific **time and place**.

This domain therefore stresses constraints that are only lightly represented in
current References.

### Candidate entities

- WorkOrder;
- Appointment;
- TimeSlot;
- Technician;
- SkillRequirement;
- Territory;
- PartDemand;
- Visit;
- CustomerCommitment.

### New semantic pressure

- resource capability/skill matching;
- geographic eligibility;
- appointment windows rather than point-in-time due dates;
- travel/setup time;
- customer-confirmed commitments;
- reschedule history;
- no-access / no-show paths;
- part availability plus technician availability.

### Strong first slice

One installation WorkOrder requiring:

- a skill-certified technician;
- a customer appointment window;
- one required part;
- rescheduling after a no-access event.

### Architectural question it can falsify

Whether current resource APIs are sufficient when capacity alone is not enough
and eligibility is multi-dimensional.

---

## Frontier 4 — Hospitality / Reservations

### Why it is useful

Reservations create temporal ownership of capacity before consumption.

A room, seat, or unit can be:

- available;
- held;
- confirmed;
- checked in;
- no-show;
- cancelled;
- released.

This differs from ordinary resource reservations because ownership spans a
future interval.

### Candidate entities

- Reservation;
- InventoryUnit;
- AvailabilityWindow;
- Hold;
- Stay;
- Cancellation;
- NoShowOccurrence.

### New semantic pressure

- interval capacity;
- expiring holds;
- overbooking policy;
- cancellation windows;
- no-show semantics;
- future ownership versus current physical use.

### Strong first slice

A hotel-like reservation with an expiring hold, confirmation, one cancellation,
and one no-show recovery path.

### Architectural question it can falsify

Whether SOSE needs a durable **temporal-capacity** primitive or whether interval
ownership belongs entirely in domain logic over existing scheduler/resource
mechanics.

---

## Frontier 5 — Warehouse / Fulfillment

### Why it remains useful despite P2P and Logistics

The novel part is not another purchase order. It is event-level inventory
visibility and allocation.

GS1 EPCIS 2.0 is a useful grounding source because it models supply-chain
visibility as events describing what happened, where, when, and in what business
context.

### Candidate entities

- FulfillmentOrder;
- Allocation;
- PickTask;
- HandlingUnit;
- InventoryLot;
- Shipment;
- InventoryEvent occurrence.

### New semantic pressure

- allocation versus physical possession;
- lot/serial identity;
- partial allocation;
- substitution;
- pick/pack/ship event lineage;
- corrections to inventory projection without deleting occurrence history.

### Strong first slice

A multi-line order with partial lot allocation, one substitution, pick/pack/ship
occurrences, and restart recovery after committed allocation but before picking.

### Architectural question it can falsify

Whether Store/Container abstractions cleanly distinguish:

- projected inventory balance;
- allocation ownership;
- immutable movement evidence.

---

## Frontier 6 — Subscription / SaaS

### Why it is lower priority

Subscription systems are commercially important, but much of their lifecycle
overlaps existing Telecom, O2C, Cards, and Credit semantics.

It becomes valuable only when the slice is chosen around genuinely novel
semantics.

### Candidate semantic pressure

- entitlement versus commercial subscription;
- future-effective plan change;
- scheduled renewal;
- cancellation-at-period-end;
- proration;
- trial conversion;
- grace period.

### Reason to defer

Without a carefully chosen slice, this risks becoming a recombination of:

- Telecom long-lived service inventory;
- Cards payment lifecycle;
- O2C receivables;
- scheduled work.

Proration would also reopen Money, which should happen only after another domain
demonstrates a repeated arithmetic contract.

---

## Recommended sequence

### Telecommunications — promoted

Telecommunications is now a Reference implementation covering service inventory,
non-rated usage, and assurance separation. Further Telecom breadth should be
added only when it introduces new semantic pressure.

### Energy / Utilities

Next strongest candidate. It adds immutable measurements, event windows,
population targeting, and outage/restoration semantics.

### Public Transit / Rail

Adds schedule-versus-realtime projection, ordered delay propagation, freshness,
and repeated observational occurrences.

### Field Service

Adds multi-dimensional resource eligibility, location, and appointment windows.

### Hospitality / Reservations

Use specifically to test interval ownership and expiring holds.

### Warehouse / Fulfillment

Use when we want to challenge inventory projection versus immutable movement
history.

### Subscription / SaaS

Defer until there is a specific semantic question worth testing.

---

## Domain stopping rule

Stop extending a Reference when all of the following are true:

1. its core domain invariants are explicit;
2. happy and representative sad paths are executable;
3. restart equivalence covers its important durable boundaries;
4. the domain has exercised the new semantic pressure that justified adding it;
5. remaining missing features are industry breadth rather than architecture
   evidence.

At that point, move to the next domain.

## Framework extraction rule

Do not promote a new core abstraction because one Reference needs it.

Promote only when:

1. at least two materially different domains need the same semantic contract;
2. local implementations repeat non-trivial lifecycle behavior;
3. restart/race semantics are the same;
4. the abstraction can be named without erasing domain meaning;
5. executable conformance tests can state what the abstraction guarantees.

This rule applies to Money, temporal capacity, qualified resources, occurrence
projection, and any future cross-domain primitive.

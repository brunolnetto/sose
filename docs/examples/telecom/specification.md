# Telecommunications Reference Domain Specification

## 1. Purpose and grounding

This Reference models a postpaid mobile service as durable operational truth
across commercial ordering, technical fulfillment, service inventory, usage,
and assurance.

The model is grounded in the separation used by TM Forum Open APIs and ODA use
cases:

- TMF622 Product Ordering for commercial product orders;
- TMF641 Service Ordering for technical service-order fulfillment;
- TMF638 Service Inventory for the long-lived service instance;
- TMF640 Service Activation/Configuration for asynchronous activation work;
- TMF635 Product Usage for rated or non-rated usage occurrences;
- TMF621 Trouble Ticket for issue-management lifecycle;
- TMF642 Alarm Management for network fault/alarm lifecycle.

TM Forum's postpaid-mobile ODA use case explicitly spans product order down to
network activation across 4G/5G and physical/pre-provisioned/eSIM variants.
SOSE keeps that architectural separation while modeling only the durable
semantics needed to exercise the runtime.

## 2. Scope

Durable entities:

- `ProductOrder`;
- `ServiceOrder`;
- `SubscriptionService`;
- `UsageRecord`;
- `NetworkAlarm`;
- `TroubleTicket`.

The executable Reference uses one postpaid-mobile ProductOrder with 5G access
and eSIM delivery characteristics.

Out of scope:

- vendor/network-element command syntax;
- SIM inventory logistics;
- number portability;
- roaming settlement;
- mediation pipelines;
- rating, charging, taxation, invoicing, and payment;
- full catalog/configuration modeling.

These may become later domain extensions only when they introduce new semantic
pressure on SOSE.

## 3. StateCharts

ProductOrder:

    captured -> acknowledged -> in_progress -> completed
       \             \              \
        +--------------+---------------> cancelled

ServiceOrder:

    pending -> accepted -> provisioning -> completed
                 \             \
                  +---------------> failed

SubscriptionService:

    designed -> provisioning -> activation_ready -> active
                                                   -> suspended -> active
                                                   -> terminated
                                      suspended -> terminated

UsageRecord:

    captured -> committed

NetworkAlarm:

    raised -> acknowledged -> cleared
       \---------------------> cleared

TroubleTicket:

    open -> acknowledged -> resolved -> closed
      \---------------------> resolved

All transitions are orchestration-gated.

## 4. Fulfillment semantics

Commercial acceptance does not itself activate a network service.

1. ProductOrder is acknowledged and enters in-progress fulfillment.
2. ProductOrder decomposes into a technical ServiceOrder.
3. SubscriptionService is created as designed inventory truth.
4. ServiceOrder is accepted.
5. ServiceOrder and SubscriptionService enter provisioning.
6. ScheduledWork represents asynchronous activation eligibility.
7. When the scheduled command fires, SubscriptionService becomes
   `activation_ready`.
8. Activation still requires provisioning availability and durable
   `provisioning_worker` capacity.
9. Only after capacity is acquired does SubscriptionService become active.
10. ServiceOrder and ProductOrder are reconciled to completed.

This preserves the SOSE rule that scheduled time is eligibility rather than
authority to claim a scarce operational resource.

## 5. Long-lived service inventory

SubscriptionService is intentionally independent from ProductOrder and
ServiceOrder.

The orders become terminal after fulfillment while the service remains active
and can later become:

- suspended by an assurance incident;
- restored after fault clearance;
- terminated by a future lifecycle extension.

This distinction is central to telecom: an order is work to change service
inventory, not the service inventory itself.

## 6. Usage semantics

UsageRecord is an immutable occurrence associated with an active service.

The current Reference stores:

- deterministic sequence identity;
- service identity;
- quantity;
- measurement unit;
- `rated=False`.

A repeated observation with the same identity and measurement is idempotent.
The same identity with a conflicting measurement is rejected.

The Reference does not rate usage. TMF635 supports both rated and non-rated
usage, so durable non-rated usage is sufficient evidence for this slice without
reopening the Money abstraction.

## 7. Assurance semantics

A network incident produces two related but independent durable facts:

- NetworkAlarm: network/fault evidence;
- TroubleTicket: operational/customer issue-management evidence.

Raising an alarm against an active service suspends the SubscriptionService.
Acknowledgement is explicit on both alarm and ticket. Restoration requires:

1. alarm clearance;
2. ticket resolution and closure;
3. explicit SubscriptionService restore transition.

The service is not restored merely because a ticket string changed or because a
backend callback fired.

## 8. Invariants

TEL-01 — ProductOrder, ServiceOrder, and SubscriptionService are distinct
durable entities with independent lifecycles.

TEL-02 — Scheduled activation time is eligibility, not activation authority.

TEL-03 — Activation requires durable provisioning-resource ownership.

TEL-04 — A ProductOrder may complete while its SubscriptionService remains a
long-lived active entity.

TEL-05 — Usage requires an active SubscriptionService.

TEL-06 — Usage identity is deterministic and replay-safe.

TEL-07 — Conflicting re-observation of the same usage identity is rejected.

TEL-08 — Usage is non-rated in this Reference; quantity must not be interpreted
as money.

TEL-09 — NetworkAlarm and TroubleTicket are distinct assurance facts.

TEL-10 — An active service is suspended when its modeled incident is raised.

TEL-11 — Restoration requires durable alarm/ticket reconciliation rather than
backend state.

TEL-12 — Scenario effects change prerequisite availability, not business state
directly.

TEL-13 — Restart equivalence holds across pending activation ScheduledWork and
committed UsageRecord replay.

## 9. Scenario

`provisioning_outage_scenario` temporarily sets
`telecom.provisioning.available=False`.

If activation eligibility occurs during the outage:

- the service remains `activation_ready`;
- no fake provisioning ResourceDemand is created;
- after the finite scenario ends, ordinary reconciliation acquires capacity and
  activates the service.

## 10. Crash/restart boundaries

Executable restart evidence covers:

1. SubscriptionService(provisioning) with activation ScheduledWork pending;
2. backend rebuild before the scheduled activation command fires;
3. activation-ready state followed by normal resource acquisition;
4. committed UsageRecord followed by rebuild and replay of the same occurrence.

A future extension should add post-activation crash boundaries around assurance
reconciliation if that path gains scarce resources or delayed work.

## 11. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| durable entities | entities.py + topology tests | implemented |
| StateCharts | statecharts.py + topology tests | implemented |
| product/service decomposition | happy path | implemented |
| asynchronous activation | ScheduledWork + restart test | implemented |
| provisioning capacity | Resource + scenario test | implemented |
| long-lived service inventory | happy/sad paths | implemented |
| immutable non-rated usage | happy/sad/restart tests | implemented |
| alarm/ticket separation | sad-path assurance test | implemented |
| service suspension/restoration | assurance test | implemented |
| finite provisioning scenario | scenario test | implemented |

## 12. Promotion decision

Current status: **Reference implementation**.

Promotion is based on executable evidence for order decomposition, asynchronous
activation eligibility, resource-gated provisioning, long-lived service
inventory, immutable replay-safe usage, alarm/ticket assurance separation,
finite provisioning outage behavior, and restart equivalence.

The next telecom extension should be chosen only if it adds semantic pressure.
Likely candidates are SIM/resource inventory, number portability, rated usage,
or recurring charging—not more workflow breadth for its own sake.

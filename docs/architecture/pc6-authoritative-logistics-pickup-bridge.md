# PC6 — Authoritative Logistics pickup bridge (PRD / TRD / ADR)

**Status:** opt-in, bounded falsification increment for #406. NOT scientific promotion.

## PRD — finite courier ownership

When a PC6 `composition.deliver_shipment` intent is mapped to a shared, finite
`pickup_courier` pool, the statechart's actual `pickup` transition must
consume **that** PostgreSQL-authoritative temporal reservation, not allocate
another independent SimPy courier. A worker without a live booking must not
perform the transition. Insufficient capacity must keep the accepted Command
durable and record a deterministic retry, without starving another domain.

Legacy Logistics runs and all existing frozen v1 experiment fixtures remain
unchanged unless the resource mapping is explicitly activated.

## TRD — causal admission → actual transition → release

1. `TradingCustomerRecoveryRunner` resolves the physical pickup demand from
   durable Logistics shipment state and scheduled work. For a newly-created
   shipment it reserves no earlier than the reference one-hour pickup delay.
   The PostgreSQL coordinator books the immutable effect ID, organization,
   pool, boundary-message cause, and half-open time interval *before* domain
   execution. Resource waiting is already durably implemented in #438.
2. The runner obtains the current authoritative booking after admission, then
   explicitly passes it through `_execute_pending` and `_execute_intent`.
   The initial pickup schedule aligns with its authoritative booking start;
   a resumed schedule may be delayed until a later admitted slot.
3. `logistics.reconcile_pickup(..., authoritative_pickup=booking)` verifies
   the **live** PostgreSQL record, status, identity and `start <= backend.now
   < end` at the transition site. It holds the ledger's **pool-scoped
   transactional lock through the nested Engine UoW commit**. Release, failure
   and preemption cannot invalidate the booking in the read/dispatch gap.
   Any disagreement fails closed before dispatch. With a valid booking, SimPy
   drives the statechart without a second
   `engine.resources.ensure_requested/withdraw` for pickup.
4. The existing BusinessEffectApplied certificate and `IntentResourceCoordinator`
   completion perform idempotent release. After death between certificate and
   release, the existing #438 certified-effect reconciler releases the same
   reservation and does not adopt resources of unrelated jobs. The same writer
   epoch fencing applies to domain UoWs and coordinator writes.

## ADR — an explicitly limited migration, not a universal bridge

- The opt-in mapping for `composition.deliver_shipment` must resolve to a
  `pickup_courier` pool. Engine/SimPy resource definitions remain the
  *legacy* source for unmapped Logistics executions, not a second capacity
  ledger for mapped pickups.
- Booking is an *interval* for the modeled pickup transition, **not** a claim
  that the entire shipment's delivery journey exclusively occupies a courier.
  Current coordinator releases this modeled interval at the end of the
  certified effect. That is a historical modeled end time, not a wall-clock
  release.
- This change does not automatically translate legacy resource requests into
  authoritative bookings; mixing mapped and unmapped customers over a
  supposedly identical physical pool is **not** proven safe.
- The generic shared `SimulationPosition` and the opt-in per-organization
  resource clock are distinct. A concurrent external advancement that moves
  the backend beyond an already-booked pickup interval is rejected, **not**
  silently fixed by advancing another organization's clock or enlarging a
  finite reservation.
- Multi-commit domain statecharts, other logistics resources, SimPy scenario
  unavailability, preemption/failure compensation, PG failover, network loss,
  and independent multi-organization replay remain explicit research gates.

## Test and promotion gates

- PostgreSQL falsification: no booking, forged owner, or out-of-window grant
  cannot pick up; valid grant transitions once and creates no second SimPy
  pickup allocation; a competing release waits for the transactionally pinned
  domain use; two idempotent releases emit exactly one release event.
- PostgreSQL restart: persisted business state and booking remain recoverable.
- Existing #438 wait/correlation/recovery tests, concurrent real PC6
  statecharts, full Python 3.12–3.14 matrix, coverage >=95%, dedicated
  PostgreSQL/chaos, runtime benchmarks and frozen v1 historical replay.
- #406 stays open. This PR must remain unmerged until exact-head gates are
  observed, review findings addressed, and the absence of regressions proved.

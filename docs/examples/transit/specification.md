# Public Transit / Rail Reference Domain Specification

## 1. Purpose and standards grounding

GTFS Schedule describes planned transit service. GTFS Realtime augments that
plan with Trip Updates, Vehicle Positions, and Service Alerts.

This Reference uses those boundaries to challenge a SOSE assumption that is
easy to blur in operational systems:

> plan, observation, and current projection are different durable facts.

The Reference is standards-grounded but intentionally not a GTFS
serialization/parser implementation.

## 2. Scope

Durable entities:

- `Vehicle`;
- `ScheduledTrip`;
- `TripUpdateOccurrence`;
- `VehiclePositionOccurrence`;
- `ServiceAlert`.

The initial block contains two ordered ScheduledTrips served by one Vehicle.

Out of scope:

- fare products and ticketing;
- passenger counting;
- route geometry / shape matching;
- platform and track capacity;
- maintenance;
- detours and replacement trips;
- full StopTimeUpdate lists;
- GTFS protobuf generation.

Those belong in later slices only if they introduce new architectural pressure.

## 3. StateCharts

Vehicle:

    available -> in_service -> available
         -> retired

ScheduledTrip:

    planned -> running -> completed
                         +-----------> cancelled

TripUpdateOccurrence:

    captured -> committed

VehiclePositionOccurrence:

    captured -> committed

ServiceAlert:

    scheduled -> active -> cleared
                           +-----------> cancelled

All transitions are orchestration-gated.

## 4. Planned schedule versus realtime projection

A ScheduledTrip owns immutable planned boundaries:

- `scheduled_start_at`;
- `scheduled_end_at`.

Realtime updates do not rewrite them.

The same entity separately owns the current operational projection:

- `projected_start_at`;
- `projected_end_at`;
- `direct_delay_seconds`;
- `block_delay_seconds`;
- `current_delay_seconds`;
- `latest_update_sequence`.

The effective delay is the maximum of direct realtime evidence for that trip and
the delay required by availability of the physical vehicle from the previous
trip in the block.

A TripUpdateOccurrence is immutable evidence. A newer committed update may
advance the projection; an older occurrence remains history and must not roll
the projection backward.

## 5. Vehicle block causality

The Reference uses one physical Vehicle for two trips in the same block.

The first trip has a planned layover before the second. A delay on trip A is
absorbed while the projected completion still precedes trip B's scheduled
start.

Only the unabsorbed overlap propagates downstream.

Example:

- trip A scheduled completion: 10:00;
- trip B scheduled departure: 10:15;
- trip A receives +20 minutes;
- trip A projected completion: 10:20;
- trip B projection becomes 10:20, therefore +5 minutes.

Pending ScheduledWork for affected trip boundaries is replaced by the new
durable projection.

## 6. Vehicle position semantics

VehiclePositionOccurrence is immutable observational evidence.

Its identity includes:

- physical vehicle;
- trip;
- occurrence sequence.

It also records `observed_at`, which is the measurement timestamp.

Occurrence arrival order does not define current truth. A late-arriving older
measurement is committed to history but does not replace
`Vehicle.latest_position_id` or `current_stop_sequence`.

New position evidence requires:

- ScheduledTrip(running);
- Vehicle(in_service);
- that Vehicle durably assigned to that trip.

Replay of already persisted evidence remains possible after the trip moves on.

## 7. Realtime availability scenario

`realtime_feed_outage` temporarily makes new realtime ingestion unavailable.

During the outage:

- no fake ScheduledTrip transition occurs;
- new TripUpdate / VehiclePosition evidence is not created;
- already durable realtime identities remain replayable.

When the scenario ends, ordinary ingestion resumes.

## 8. Service Alert semantics

A ServiceAlert owns:

- an immutable affected-trip scope;
- a durable start boundary;
- a durable end boundary.

ScheduledWork activates and clears the alert.

The alert describes a disruption interval; it does not itself mutate the
ScheduledTrip projection. Trip-specific delay/cancellation evidence remains a
separate concern.

## 9. Invariants

TRN-01 — Planned schedule boundaries are not overwritten by realtime updates.

TRN-02 — TripUpdateOccurrence is immutable replay-safe evidence.

TRN-03 — Only a newer update sequence may advance the current trip projection.

TRN-04 — Downstream block delay equals only the portion not absorbed by the
planned gap between trips and cannot erase a larger direct delay already
supported by that trip's own realtime evidence.

TRN-05 — Rescheduled trip boundaries are durable ScheduledWork and survive
runtime rebuild.

TRN-06 — VehiclePositionOccurrence is immutable evidence.

TRN-07 — Observation time, not ingestion order, determines the latest vehicle
position projection.

TRN-08 — New position evidence requires a running trip and durable vehicle
ownership.

TRN-09 — Persisted realtime evidence can be replayed independently of later
feed availability, and replay reconciles a projection if a crash happened after
evidence commit but before projection persistence.

TRN-10 — ServiceAlert scope and time range are durable and replay-stable.

TRN-11 — A finite realtime outage alters evidence availability, not business
state directly.

TRN-12 — One physical vehicle may serve ordered trips over time without
duplicating vehicle identity.

## 10. Happy path

1. Seed one Vehicle and two planned trips in one block.
2. Persist ScheduledWork for both trip boundaries.
3. Trip A starts.
4. Vehicle ownership reconciles to trip A.
5. Commit one VehiclePositionOccurrence.
6. Trip A completes and releases the Vehicle.
7. Trip B starts with the same Vehicle identity.
8. Trip B completes and releases the Vehicle.

## 11. Representative sad paths

- trip A delay exceeds its planned inter-trip gap and propagates to trip B;
- duplicate TripUpdate identity with conflicting delay is rejected;
- stale VehiclePositionOccurrence is retained without rolling projection back;
- position evidence before trip/vehicle ownership is rejected;
- finite realtime feed outage blocks new evidence without changing trip state;
- ServiceAlert activation/clear follows its own time range.

## 12. Restart boundaries

Executable evidence covers:

1. four pending trip boundaries;
2. a committed delay update that replaces affected boundaries;
3. runtime rebuild before either trip executes;
4. trip A completion and trip B start at the persisted projected boundary;
5. VehiclePositionOccurrence(captured) persisted before commit;
6. runtime rebuild followed by replay completing the occurrence.

## 13. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| StateCharts | `test_transit_statecharts.py` | implemented |
| happy path / block identity | `test_transit_happy_path.py` | implemented |
| delay propagation | `test_transit_sad_paths.py` | implemented |
| stale observations / prerequisites | `test_transit_sad_paths.py` | implemented |
| ServiceAlert time range | `test_transit_sad_paths.py` | implemented |
| finite realtime outage | `test_transit_scenarios.py` | implemented |
| restart equivalence | `test_transit_restart_equivalence.py` | implemented |

## 14. Promotion decision

Current status: **Reference implementation**.

The slice stops here once executable evidence proves schedule-versus-projection
separation, vehicle-block delay propagation, immutable observational evidence,
staleness handling, bounded alert semantics, finite feed outage recovery, and
restart equivalence.

Full stop-level prediction lists, detours, capacity, maintenance, fares, and
passenger information remain industry breadth until they expose a new
architectural question.

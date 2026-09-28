# Public Transit / Rail — Executable Specification

## 1. Purpose

Model a minimal GTFS-grounded transit operation that challenges SOSE with
schedule-versus-realtime semantics rather than another ordinary order workflow.

## 2. Durable entities

### Vehicle
Current operational projection for one physical vehicle.

States:

`idle -> in_service -> idle`

An out-of-service branch exists for future disruption tests.

### VehicleBlock
Ordered group of trips served by the same physical vehicle.

States:

`planned -> active -> completed`

### ScheduledTrip
Static scheduled times plus durable current projected times.

States:

`planned -> in_progress -> completed`

### StopCall
Ordered stop sequence owned by a trip.

States:

`pending -> arrived -> departed`

A stop may instead become `skipped`.

### TripUpdate
Immutable occurrence carrying one identified delay observation.

States:

`captured -> committed`

### VehiclePositionOccurrence
Immutable GPS-like measurement with its own measurement timestamp.

States:

`captured -> committed`

### ServiceAlert
Passenger-facing disruption whose relevance is bounded in time.

States:

`scheduled -> active -> resolved`

## 3. Identity invariants

- the physical Vehicle ID remains stable across both trips;
- one VehicleBlock owns an ordered list of trip IDs;
- TripUpdate identity is `(trip, sequence)`;
- VehiclePositionOccurrence identity is `(vehicle, sequence)`;
- replay with the same occurrence identity and different facts is rejected;
- ServiceAlert target scope cannot change on replay.

## 4. Schedule and projection

The Reference schedule is:

| Trip | Scheduled start | Scheduled end |
| --- | --- | --- |
| trip-1 | 08:00 | 08:30 |
| trip-2 | 08:40 | 09:10 |

A trip update changes projected values, never the static scheduled values.

For downstream trips in the same block:

`downstream delay = max(0, previous projected end - downstream scheduled start)`

Therefore a 15-minute delay on trip-1 consumes the ten-minute layover and
projects trip-2 five minutes late.

## 5. Observation versus projection

VehiclePositionOccurrence is historical evidence.

Vehicle is the current projection and may point to the latest committed
observation. An out-of-order older observation remains durable but does not move
the latest-position pointer backwards.

The externally useful realtime view is fresh only while:

`as_of - measured_at <= 90 seconds`

Staleness suppresses the position from the view; it does not delete the
occurrence.

## 6. Service alerts

A ServiceAlert has:

- a stable incident key;
- one or more affected trips;
- a start instant;
- an end instant.

Activation and resolution are durable ScheduledWork. Restart must reconstruct
those timers from durable truth.

## 7. Scenario semantics

A finite tracking outage sets `transit.tracking.available = false`.

While unavailable:

- new VehiclePositionOccurrence entities are not fabricated;
- trip schedule/projection remains valid;
- already committed observations remain durable.

When tracking returns, new observations may be captured again.

## 8. Representative happy path

1. seed one vehicle, one two-trip block, and ordered StopCalls;
2. begin trip-1;
3. commit a vehicle position;
4. commit a 15-minute TripUpdate;
5. verify trip-2 receives five minutes of propagated delay;
6. complete trip-1;
7. begin and complete trip-2;
8. verify block completion and vehicle release.

## 9. Representative sad paths

- starting trip-2 before trip-1 completes is illegal;
- negative delay is rejected;
- invalid coordinates are rejected;
- occurrence replay with changed facts is rejected;
- alert with invalid duration or out-of-block target is rejected;
- stale position is excluded from the realtime view.

## 10. Restart equivalence

Restart evidence must cover:

- committed delay projection;
- immutable position occurrence and current pointer;
- pending ServiceAlert activation/resolution;
- no duplicate occurrences after replay.

## 11. Executable evidence

| Requirement | Evidence |
| --- | --- |
| statecharts | `test_public_transit_statecharts.py` |
| happy path and block delay propagation | `test_public_transit_happy_path.py` |
| sad paths / freshness | `test_public_transit_sad_paths.py` |
| immutable realtime occurrences | `test_public_transit_occurrences.py` |
| finite tracking outage | `test_public_transit_scenarios.py` |
| restart equivalence / ScheduledWork | `test_public_transit_restart_equivalence.py` |

## 12. Promotion decision

Current status: **Reference implementation**.

The slice is complete when the executable evidence above is green. Further
transit breadth should be added only if it challenges a new architectural
assumption.

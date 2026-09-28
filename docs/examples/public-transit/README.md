# Public Transit / Rail Reference

## Status

**Reference implementation.**

This Reference is grounded in GTFS Schedule and GTFS Realtime vocabulary. It is
not a complete transit-planning system.

The executable slice focuses on the architectural distinction between:

- the static schedule;
- the durable current operational projection;
- immutable realtime observations;
- externally relevant disruption windows.

## Domain grounding

GTFS Schedule uses `block_id` to identify one or more sequential trips that may
be served by the same vehicle. GTFS Realtime separates Trip Updates, Vehicle
Positions, and Service Alerts, and its best practices emphasize stable realtime
identifiers and freshness.

SOSE models those boundaries as:

- `VehicleBlock`: ordered ownership of sequential trips by one vehicle;
- `ScheduledTrip`: planned trip plus current projected start/end;
- `StopCall`: ordered planned stop call plus current projected time;
- `TripUpdate`: immutable delay occurrence;
- `VehiclePositionOccurrence`: immutable measured position;
- `Vehicle`: current projection pointing to the latest committed observation;
- `ServiceAlert`: bounded passenger-facing disruption interval.

## Executable pressure

The first block has two trips:

- trip 1: 08:00–08:30;
- ten-minute scheduled layover;
- trip 2: 08:40–09:10.

A 15-minute delay on trip 1 projects its end to 08:45. Because only ten minutes
of layover are available, trip 2 receives five minutes of propagated delay.

That distinction is intentional: delay propagation is derived from the ordered
block and schedule, not copied blindly from one trip to the next.

Vehicle positions are immutable occurrences. The Vehicle entity stores only a
pointer to the newest measured observation, so historical evidence is not
rewritten when the operational projection advances.

Realtime publication treats the latest vehicle position as stale after 90
seconds, matching the freshness pressure in GTFS Realtime best practices.

## References

- GTFS Schedule reference: https://gtfs.org/documentation/schedule/reference/
- GTFS Realtime Trip Updates: https://gtfs.org/documentation/realtime/feed-entities/trip-updates/
- GTFS Realtime Vehicle Positions: https://gtfs.org/documentation/realtime/feed-entities/vehicle-positions/
- GTFS Realtime Service Alerts: https://gtfs.org/documentation/realtime/feed-entities/service-alerts/
- GTFS Realtime best practices: https://gtfs.org/documentation/realtime/realtime-best-practices/

## Stopping rule

The Reference stops after proving:

- one physical vehicle serves two ordered trips in one block;
- first-trip delay propagates through available layover into the second trip;
- TripUpdate and VehiclePosition evidence is immutable and replay-safe;
- current vehicle projection is distinct from occurrence history;
- position freshness can invalidate an externally published view without
  deleting history;
- ServiceAlert activation/resolution survives restart;
- finite tracking outage scenarios do not create fake observations.

Fare collection, network routing, crew rostering, and full GTFS feed generation
are breadth, not required architecture evidence for this slice.

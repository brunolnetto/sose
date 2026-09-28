# Public Transit / Rail

Status: **Reference implementation**

This Reference models the separation between planned transit service and
realtime operational evidence.

The executable slice deliberately separates:

- `ScheduledTrip` — durable planned service and its current projection;
- `Vehicle` — one physical vehicle reused across an ordered block of trips;
- `TripUpdateOccurrence` — immutable realtime delay evidence;
- `VehiclePositionOccurrence` — immutable timestamped position evidence;
- `ServiceAlert` — a bounded disruption notice over affected trips.

The vocabulary is grounded in GTFS Schedule / GTFS Realtime concepts. SOSE does
not implement the GTFS wire format and does not claim GTFS compliance; the
standard is used to keep schedule, realtime prediction, vehicle observation,
and alert scope semantically distinct.

The first slice proves downstream block-delay propagation, stale observation
handling, replay-safe realtime identities, finite feed outages, bounded alerts,
and restart equivalence.

See [specification.md](specification.md) for the executable contract.

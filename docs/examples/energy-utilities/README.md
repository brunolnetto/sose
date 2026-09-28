# Energy / Utilities

Status: **Reference implementation**

This Reference models utility operational truth around metering, service
interruption/restoration, and demand response.

The first executable slice deliberately separates:

- `ServicePoint` — supply/service state at a premise;
- `Meter` — the active measuring device;
- `MeterReading` — immutable interval measurement occurrence;
- `Outage` — durable interruption/restoration evidence;
- `DemandResponseEvent` — a bounded control/event window;
- `DemandResponseParticipation` — service-point participation in that event.

The vocabulary is grounded in IEC 61968-9:2024 meter-reading/control integration
and OpenADR 3 program/event/report/resource concepts. The Reference does not
claim standards compliance; it uses those standards to avoid inventing
unrealistic domain boundaries.

Tariffs, billing, settlement, market clearing, and power-flow simulation are out
of scope. The point of this slice is to stress immutable measurements,
corrections, outage ownership, population-scoped timed event windows, and
restart semantics.

See [specification.md](specification.md) for the executable contract.

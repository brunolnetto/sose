# Energy / Utilities Reference Domain Specification

## 1. Purpose and standards grounding

This Reference exercises utility semantics that differ materially from SOSE's
order, payment, and service-workflow domains.

IEC 61968-9:2024 defines information exchange for meter reading and control and
explicitly identifies interval/time-based usage and production data, outage
management, service interruption/restoration, demand response, customer billing,
and work management as consumers of meter information.

OpenADR 3 uses a REST/OpenAPI information model whose primary objects include
programs, events, reports, subscriptions, VENs, and resources. Its user guidance
describes event-driven demand-management interactions.

SOSE does not reproduce those standards. It uses their entity separation to
define a small executable utility slice.

## 2. Scope

Durable entities:

- `ServicePoint`;
- `Meter`;
- `MeterReading`;
- `Outage`;
- `DemandResponseEvent`;
- `DemandResponseParticipation`.

Out of scope:

- tariff calculation;
- invoicing/payment;
- energy-market settlement;
- load-flow/network simulation;
- DER-specific control functions;
- multi-premise aggregation;
- meter communications protocols.

Those should be added only if they expose semantics not already proven here.

## 3. StateCharts

ServicePoint:

    energized -> interrupted -> energized
         \          \
          +-----------> disconnected

Meter:

    active -> retired

MeterReading:

    captured -> committed

Outage:

    reported -> confirmed -> restoring -> restored

DemandResponseEvent:

    scheduled -> active -> completed
        \          \
         +-----------> cancelled

DemandResponseParticipation:

    eligible -> active -> completed
       |          \
       |           -> opted_out
       +-> missed
       +-> opted_out

All transitions are orchestration-gated.

## 4. Metering semantics

A MeterReading is an immutable occurrence, not a mutable current-value field.

Its durable identity includes:

- meter identity;
- interval end;
- correction ordinal.

A replay of the same identity and measurement is idempotent. A replay with a
conflicting value is rejected.

A correction does not overwrite the prior occurrence. It creates a new
MeterReading with:

- `quality="corrected"`;
- a positive correction ordinal;
- `supersedes_reading_id` pointing to a committed reading for the same meter
  and interval.

This preserves both the original evidence and the corrected projection lineage.

The crash boundary between saving `MeterReading(captured)` and dispatching
`commit` is recoverable: replay resumes the deterministic commit transition.

## 5. Outage semantics

Outage is independent from ServicePoint state.

Reporting an unresolved outage:

1. persists deterministic Outage evidence;
2. confirms the outage;
3. adds its incident key to `ServicePoint.open_outage_keys`;
4. interrupts an energized ServicePoint.

Multiple outages may overlap.

Restoring one outage removes only that key. ServicePoint is restored to
`energized` only when no durably owned outages remain.

Replaying an already-restored outage does not interrupt service again.

## 6. Demand-response semantics

DemandResponseEvent owns the event window.

ScheduledWork makes two time boundaries durable:

- event start;
- event finish.

DemandResponseParticipation is separate from the event clock. When the event is
active, participation begins only if:

- ServicePoint is energized;
- demand-response communications/prerequisites are available.

If the event completes while participation is still eligible, the participation
is marked `missed`. An active participation becomes `completed`.

This distinction is intentional: the passage of time controls the event window;
it does not directly assert that a participant responded.

## 7. Scenario

`demand_response_communications_outage` temporarily sets
`energy.dr.available=False` for two hours.

The Reference schedules a demand-response event whose window overlaps that
outage. At event start:

- DemandResponseEvent becomes active;
- Participation remains eligible;
- no fake business transition is emitted.

When communications recover while the event is still active, ordinary
reconciliation begins participation. Event completion later closes that
participation.

## 8. Invariants

ENE-01 — MeterReading is immutable occurrence evidence.

ENE-02 — Corrections create new reading identity and preserve a supersedes link.

ENE-03 — A corrected reading may supersede only a committed reading from the
same meter and interval.

ENE-04 — Conflicting replay of one MeterReading identity is rejected.

ENE-05 — MeterReading(captured) replay resumes commit after restart.

ENE-06 — Outage and ServicePoint are distinct durable facts.

ENE-07 — ServicePoint remains interrupted while any open outage is durably
owned.

ENE-08 — Replaying a restored outage is idempotent and does not interrupt
service again.

ENE-09 — ScheduledWork defines demand-response event boundaries, not participant
response.

ENE-10 — Participation requires an energized ServicePoint and available
prerequisites.

ENE-11 — A finite scenario changes prerequisite availability rather than
directly changing domain state.

ENE-12 — Restart equivalence preserves both pending event boundaries and
immutable measurement identity.

## 9. Happy path

1. Seed energized ServicePoint and active Meter.
2. Commit one actual interval reading.
3. Schedule a demand-response event.
4. Event start becomes due.
5. Eligible participation begins.
6. Event finish becomes due.
7. Participation and event finish consistently.

## 10. Representative sad paths

- conflicting replay of a reading identity;
- correction referencing missing/non-committed evidence;
- overlapping outages;
- replay of a restored outage;
- demand-response communication outage during an active event window.

## 11. Restart boundaries

Executable evidence covers:

1. two pending ScheduledWork boundaries for a future demand-response event;
2. runtime rebuild before event start;
3. event start and participation reconciliation after rebuild;
4. event finish after rebuild;
5. MeterReading(captured) persisted before commit dispatch, followed by rebuild
   and replay.

## 12. Executable evidence

| Specification area | Evidence | Status |
|---|---|---|
| StateCharts | `test_energy_utilities_statecharts.py` | implemented |
| happy path | `test_energy_utilities_happy_path.py` | implemented |
| immutable reading/correction | `test_energy_utilities_sad_paths.py` | implemented |
| outage ownership/restoration | `test_energy_utilities_sad_paths.py` | implemented |
| demand-response scenario | `test_energy_utilities_scenarios.py` | implemented |
| restart equivalence | `test_energy_utilities_restart_equivalence.py` | implemented |

## 13. Promotion decision

Current status: **Reference implementation**.

The slice is complete when its executable evidence proves immutable measurement
lineage, multi-outage restoration ownership, bounded demand-response event
semantics, finite scenario recovery, and restart equivalence.

Further utility breadth should stop here unless a new slice challenges an
unproven architectural assumption.

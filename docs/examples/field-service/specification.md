# Field Service / Workforce — Executable Specification

## 1. Purpose

Test whether SOSE can keep multi-dimensional workforce eligibility in domain
logic while reusing core durable mechanics for capacity, scheduled boundaries,
Store selection, replay, and restart.

## 2. Durable entities

### WorkOrder

States:

`ready -> scheduled -> in_progress -> completed`

No-access branch:

`in_progress -> reschedule_required -> scheduled`

### Appointment

States:

`proposed -> confirmed -> in_progress -> completed`

Exception branches:

- `in_progress -> no_access`;
- `confirmed -> missed`;
- proposed/confirmed -> cancelled.

A replacement appointment is a new durable entity with
`replaces_appointment_id`; the failed appointment is not rewritten.

### Technician

States:

`available -> assigned -> available`

Attributes carry skills, territories, and durable future bookings.

### VisitOccurrence

States:

`captured -> committed`

Visit outcomes are immutable replay-safe evidence.

## 3. Eligibility

A technician is eligible only when:

1. all WorkOrder required skills are present;
2. the WorkOrder territory is supported;
3. the proposed appointment interval does not overlap an existing booking.

The domain selects the technician. Only then does execution acquire the
technician-specific core Resource.

## 4. Appointment ownership

A confirmed Appointment owns:

- place;
- start/end time;
- technician;
- replacement lineage.

ScheduledWork owns the start and end/miss boundaries. This is future semantic
ownership, not a generic Resource reservation.

## 5. Part ownership

The required part is selected from `field_parts` through durable Store
selection. The WorkOrder request ID is stable across a no-access reschedule, so
the same selected part remains owned rather than being consumed twice.

## 6. No-access semantics

A no-access visit:

1. commits immutable VisitOccurrence evidence;
2. ends the active Appointment as `no_access`;
3. moves WorkOrder to `reschedule_required`;
4. releases technician execution capacity;
5. preserves the part selection;
6. permits a replacement Appointment with explicit lineage.

## 7. Invariants

FS-01 — Appointment time/place is durable domain truth.

FS-02 — Technician skill/territory/window eligibility is decided before Resource
acquisition.

FS-03 — Technician Resource identity is chosen by the domain, while core owns
request/reservation/release mechanics.

FS-04 — Overlapping bookings prevent double assignment of the same qualified
technician.

FS-05 — Required-part StoreGetResult survives retry/restart and no-access
rescheduling.

FS-06 — VisitOccurrence identity cannot be replayed with a different outcome.

FS-07 — A no-access appointment remains historical evidence; rescheduling
creates a new Appointment.

FS-08 — Pending appointment boundaries survive backend rebuild.

FS-09 — A finite dispatch outage defers work start without inventing technician
ownership or changing the appointment evidence.

## 8. Happy path

1. seed WorkOrder and two technicians;
2. seed one required part;
3. propose one customer appointment;
4. select the only skill/territory/window eligible technician;
5. confirm and schedule the appointment;
6. at appointment start, reserve the selected part and technician capacity;
7. commit successful VisitOccurrence;
8. complete Appointment and WorkOrder;
9. release technician.

## 9. Representative sad paths

- invalid appointment interval;
- no technician satisfying skill/territory/window;
- overlapping technician booking;
- missing part;
- dispatch outage;
- conflicting VisitOccurrence replay;
- no-access followed by replacement appointment.

## 10. Restart equivalence

Restart evidence covers a confirmed replacement appointment after a no-access
visit. Its boundaries, part ownership, technician booking, and WorkOrder
reschedule state must survive rebuild; execution then resumes at the replacement
window and completes without duplicate selection.

## 11. Executable evidence

| Requirement | Evidence |
| --- | --- |
| statecharts | `test_field_service_statecharts.py` |
| happy path | `test_field_service_happy_path.py` |
| eligibility / no-access sad paths | `test_field_service_sad_paths.py` |
| finite dispatch outage | `test_field_service_scenarios.py` |
| restart equivalence | `test_field_service_restart_equivalence.py` |

## 12. Promotion decision

Current status: **Reference implementation**.

Do not add workforce optimization or a core qualified-resource abstraction until
another materially different domain demonstrates the same contract.

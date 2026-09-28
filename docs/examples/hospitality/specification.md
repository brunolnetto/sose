# Hospitality / Reservations — Executable Specification

## 1. Purpose

Exercise durable future interval ownership independently of current runtime
resource capacity.

## 2. Durable entities

Hotel is the durable inventory index. Room is a stable inventory unit.
Reservation carries the commercial/customer commitment. RoomBooking carries
physical interval ownership. NoShowOccurrence is immutable evidence.

Reservation lifecycle:
requested -> held -> confirmed -> checked_in -> checked_out.
Exception branches are held -> expired, held|confirmed -> cancelled, and
confirmed -> no_show_recorded.

RoomBooking lifecycle:
held -> confirmed -> occupied -> completed.
Release branches are held -> expired, held|confirmed -> released, and
confirmed -> no_show_recorded.

NoShowOccurrence lifecycle: captured -> committed.

## 3. Availability

A RoomBooking blocks its room only while held, confirmed, or occupied.
Intervals overlap iff left.start < right.end and right.start < left.end.
This permits back-to-back stays while preventing double ownership.

## 4. Hold semantics

Creating a hold validates the interval, chooses one available room, persists
Reservation and RoomBooking ownership, then schedules both hold-expiry
boundaries. Expired entities remain history; availability simply stops treating
their booking as blocking inventory.

## 5. Confirmation and no-show

Confirmation cancels both hold-expiry lifecycles, transitions Reservation and
RoomBooking, then schedules no-show boundaries after arrival grace. Check-in
cancels no-show boundaries and moves inventory to occupied. If the guest never
arrives, both durable lifecycles enter no_show_recorded and one immutable
NoShowOccurrence can be committed idempotently.

## 6. Invariants

HOS-01 — Availability is derived from durable active RoomBooking intervals.

HOS-02 — Current Resource capacity is not used for future room ownership.

HOS-03 — Overlapping active bookings cannot own the same room.

HOS-04 — Hold expiry releases availability without deleting history.

HOS-05 — Confirmation replaces hold-expiry work with no-show work.

HOS-06 — Cancellation releases future inventory and removes pending boundaries.

HOS-07 — Check-in requires confirmed ownership and a timestamp inside the stay.

HOS-08 — No-show evidence is immutable and idempotent.

HOS-09 — Pending hold/no-show boundaries survive backend rebuild.

## 7. Happy path

Seed two rooms, hold one future interval, confirm it, check in during the booked
interval, check out, and verify no pending ScheduledWork remains.

## 8. Representative sad paths

Invalid interval; all rooms blocked; hold expiry; confirmed cancellation;
check-in without confirmation; check-in outside the stay interval; no-show with
immutable occurrence evidence.

## 9. Restart equivalence

Restart evidence covers a pending hold rebuilt before expiration and a confirmed
booking rebuilt before its no-show boundary.

## 10. Executable evidence

| Requirement | Evidence |
| --- | --- |
| statecharts | test_hospitality_statecharts.py |
| happy path | test_hospitality_happy_path.py |
| sad paths / invariants | test_hospitality_sad_paths.py |
| restart equivalence | test_hospitality_restart_equivalence.py |
| scheduled work | sad/restart suites |
| immutable no-show occurrence | sad/restart suites |

## 11. Promotion decision

Current status: **Reference implementation**.

Do not extract a temporal-capacity primitive solely from Field Service and
Hospitality. Compare the actual green lifecycle contracts first.

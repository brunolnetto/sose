# Hospitality / Reservations Reference

## Status

**Reference implementation.**

This Reference is grounded in hotel reservation messaging patterns documented by
OpenTravel: availability/inventory precedes reservation ownership, and booking
workflows include holds/unholds, reservations, and inventory availability.

The executable slice tests a specific SOSE question:

> does future interval ownership require a core temporal-capacity primitive, or
> can durable domain entities plus ScheduledWork express it cleanly?

## Executable slice

A hotel has two standard rooms. A reservation searches availability for an
arrival/departure interval, creates an expiring hold, may confirm before expiry,
may check in/out or cancel, and becomes a no-show if arrival grace expires.

A RoomBooking is the durable owner of the room interval. Availability is derived
from active booking intervals (held, confirmed, occupied), not from current
Resource capacity.

## Important distinction from Field Service

Field Service stores a technician's future booking as workforce-domain
eligibility evidence and uses Resource capacity only as a start-time gate.

Hospitality instead makes interval ownership a first-class RoomBooking entity.
Holds, confirmation, occupancy, release, expiry, and no-show are inventory
semantics.

The two domains therefore share interval overlap as a concept without yet
sharing enough lifecycle mechanics to justify a core temporal-capacity type.

## Stopping rule

The Reference stops once executable evidence proves overlap-safe availability,
expiring holds, confirmation replacing hold expiry, cancellation releasing
future inventory, legal check-in timing, no-show release with immutable
occurrence evidence, and restart equivalence.

Rates, taxes, payment guarantees, upgrades, channel management, housekeeping,
and revenue management remain industry breadth unless they expose a new SOSE
architecture question.

## References

- OpenTravel hotel functionality: search, availability, offers/orders, booking
  and reservations.
- OpenTravel messaging features: Holds & Unholds, Inventory, Reservations,
  Search & Availability.

# Airports

Status: **Partial**

This reference domain models an airport turnaround as coordinated durable truth
across gate ownership, ground service, baggage readiness, departure priority,
and a time-bound departure slot.

The implementation deliberately keeps the departure slot separate from departure
execution: time may make a slot eligible, but a flight still needs operational
readiness, queue ownership, tug capacity, and gate cleanup.

See [specification.md](specification.md) for the authoritative contract.

Executable evidence is present for nominal turnaround, gate hold, baggage delay,
weather slot delay, priority queue ownership, post-commit recovery, and restart
boundaries. Promotion remains pending CI and final review.

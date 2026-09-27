# Airports

Status: **Reference implementation**

This reference domain models an airport turnaround as coordinated durable truth
across gate ownership, ground service, baggage readiness, departure priority,
and a time-bound departure slot.

The implementation deliberately keeps the departure slot separate from departure
execution: time may make a slot eligible, but a flight still needs operational
readiness, queue ownership, tug capacity, and gate cleanup.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation includes nominal turnaround, gate hold and reallocation, ground-service and baggage prerequisites, weather slot delay, priority queue ownership, committed queue-selection recovery, post-commit cleanup recovery, finite scenarios, and restart equivalence.

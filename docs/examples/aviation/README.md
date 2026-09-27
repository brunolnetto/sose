# Aviation

Status: **Reference implementation**

This reference domain models aircraft rotation across two causally linked flight
legs, including scheduled departure eligibility, crew assignment, aircraft
ownership, landing inspection, AOG maintenance, spare-part issue, and
preemptive maintenance capacity.

The second leg is intentionally dependent on release of the first leg so delay
propagation is executable rather than described only in prose.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation covers nominal two-leg rotation, weather and crew delays, predecessor-release propagation, inspection pass/fail, AOG part gating, priority maintenance selection, maintenance-bay preemption, AOG delay propagation, post-commit recovery, and restart equivalence.

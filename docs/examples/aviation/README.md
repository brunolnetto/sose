# Aviation

Status: **Partial**

This reference domain models aircraft rotation across two causally linked flight
legs, including scheduled departure eligibility, crew assignment, aircraft
ownership, landing inspection, AOG maintenance, spare-part issue, and
preemptive maintenance capacity.

The second leg is intentionally dependent on release of the first leg so delay
propagation is executable rather than described only in prose.

See [specification.md](specification.md) for the authoritative contract.

Executable evidence covers nominal rotation, weather and crew delays, inspection
pass/fail, AOG part gating, maintenance-bay preemption, post-commit recovery,
and restart boundaries. Promotion remains pending CI and final audit.

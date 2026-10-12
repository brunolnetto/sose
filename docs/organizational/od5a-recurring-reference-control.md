# OD-5a reference recurrence: bounded physical source and causal delivery (non-confirmatory)

Tracker: #485 under #477. Predecessors: merged #479, #482, #484 and #486.

## Contract

An **explicit reference-only** one-step harness combines the existing authoritative SQLite/PostgreSQL operational reducer with the existing independently durable causal read-side reconciler. One call first reconciles the earliest previously committed physical completion for the selected organization; if there are no outstanding boundary phases, it commits **at most one** reference reducer action. Crash/restart reopens the same semantic state rather than replaying a physical source event. Exactly one local physical-source or boundary transition is committed per successful invocation.

This yields a mandatory cross-arm **control** and exposes the stop rule for the real #485 adapter. It is **not** the actual SOSE recurring `SimulationJob`, not a new lease-backed job scheduler, and not the native ephemeral SimPy producer. Do not promote it to production or count its passing tests as completion of #485.

## Falsifiers

- Full five-arm match between continuous and reopened recurring reference truth: complete per-org scheduler state, immutable causal DomainEvents, typed boundary messages, delivery lease/epoch and consumption, certificates and no lost reservations. Physical stage traces are cross-checked separately against `run_od5a_pilot` on native ephemeral SimPy with fixed arrivals/services.
- OS worker kill directly after the source/boundary call COMMIT, reopen from fresh SQLite connections and check complete end state against uninterrupted baseline.
- Three independent organizations share SQLite, progress interleaved without synthetic global organizational time, and converge to organization-by-organization sequential control.
- An uninitialized org cannot become an invented world.

## Remaining #485 production promotion

Native SimPy-backed production `SimulationJobState` and recurring trigger/worker lease integration, actual OS kills at physical reservation transaction boundaries and PostgreSQL network/DB restart, same-job competing worker fencing and capacity >1 contention with durable wait/retry, complete three-way physical and financial provenance, and reproducible hash-stamped evidence bundles remain **open**. OD-5a execution freeze stays `NOT_ADMITTED`. Do not run 17,920 confirmatory simulations.

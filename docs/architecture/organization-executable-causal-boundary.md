# #496 W4a — first executable owner-fenced boundary (not a full organization runner)

This is a bounded **single-store** execution slice stacked after read-side
#540 → #541 → #542. It wires the existing durable SimulationJob, BoundaryService,
Command and BusinessEffectService; it never constructs a super-StateChart.

## Executable gates

* A declared job is bound to a REAL SimulationJob whose `job_id` exactly
  equals its component's operational `owner_id`, domain matches, and
  Persistence instance is identical to this bounded executor.
* `run_job` advances one explicit job's engine and its owner-local tick/clock;
  no automatic global organization tick or implicit trigger namespace.
* `publish` only accepts an immutable, persisted owner-qualified StateChart
  DomainEvent from the **admitted** producer, with exact typed `event`
  causation, source identity/correlation/time and admitted producer→consumer
  versioned boundary.
* `consume` can only claim that exact message, atomically ACKing a durably
  staged, correctly owned/correlated downstream Command. Wrong owner, missing
  Command, wrong causal parent or rollback fails closed.
* `certify` delegates to the pre-existing BusinessEffectService, which
  requires a real terminal business entity, the unique consumed ACK and
  matching Command before recording immutable BusinessEffectApplied.
  ACK by itself is emphatically **not** an effect.

## Scientific/operational stop rule

This is **not** cross-database distributed composition. It uses the pre-existing
single-store BoundaryService transaction fence (including its existing
PostgreSQL boundary serialization). It does not solve PG two-store ownership,
independent cross-store restarts, physical shared pools, preemption or all
#496 acceptance criteria. A staged Command is not proof that a downstream
domain transition was *caused* by that Command; that stronger causal
attestation still needs real per-domain Command handling and falsifiers.
No imaginary calendars/policies/pools or scientific OD-5a execution is admitted.

A bounded test may supply an explicitly hypothetical audited ingress
manifest for a currently unsupported domain edge. That is test-only and does
not modify builtin process maturity or upgrade unverified production contracts.

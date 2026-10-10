# PC6 — Federated organizational fault-window equivalence (PRD / TRD / ADR)

**Status:** additive PostgreSQL scientific falsification for #406. Neither this experiment nor a green CI certifies all cross-domain PC6 faults.

## PRD — independent organizations under real worker death

The same two independently schedulable Logistics organizations must reach identical *durable business and physical truth* after an ungracefully terminated worker, regardless of whether that worker died immediately after the finite physical courier reservation, after its release, or after the recurring slot checkpoint. Organization B must progress on its own writer lease and logical clock while A is unavailable; no fleet-wide writer epoch or global simulated clock is allowed.

A restart must reconstruct the domain statechart and active Command, reconcile physically booked capacity from the authoritative temporal ledger, recover immutable business-effect certificates, and replay an unfinished occurrence without fabricating new causal identities. A completed occurrence must be a strict no-op on duplicate invocation.

## TRD — separately fatal causal windows

The integration test `test_pg_os_kill_at_multiple_federated_causal_commit_windows_is_equivalent` in `tests/e2e/sose/persistence/test_postgres_pc6_federated_recovery.py` runs three new child-process cuts with actual `os._exit(79)`, not raised exceptions:

| Fatal cut | Committed fact | Expected recovery |
| --- | --- | --- |
| `after_booking` | Shared PostgreSQL physical reservation and effect/causation link; original domain Command still pending | Reuse the booking; execute real Logistics statechart; certify once; release physical resource; checkpoint original trigger |
| `after_release` | Domain delivered, immutable certificate, Command removed and physical release event committed; slot checkpoint unfinished | Do not reapply the delivered effect; complete the original scheduled slot once |
| `after_checkpoint` | Real domain certificate, physical release and completed recurring slot checkpoint | Reopened worker finds no due occurrence and does not produce another event |

The existing cut `after_certificate` remains independently covered: domain certificate is committed but shared physical release has not yet happened.

All cases compare to a fresh-namespace uninterrupted baseline. The canonical comparison includes terminal domain entity versions, domain events and causation links, boundary deliveries and consumption identities, immutable effect certificates, scheduled work, complete shared physical ledger bookings and events, per-organization resource clocks, independent simulation positions, and completed job triggers. The only canonical substitution is the deployment namespace embedded in globally qualified physical booking IDs; records are re-sorted afterwards. Retries, thread identity and wall-time leases are not normalized into business facts.

A required CI artifact `pc6-federated-fault-matrix.json` reports each cut's SHA-256 canonical digest and causal evidence summary. The existing `pc6-federated-recovery.json` remains unchanged.

## ADR — chosen and deferred

**Chosen:** retain independent per-organization operational namespaces and writer epochs with one explicitly shared PostgreSQL physical resource ledger. Keep the historical single-store runner as the default where federated mode is not configured; the matrix only asserts the federated adapter mode.

**Rejected:** a test-only successful exception/rollback that would not terminate a process; rerunning in-memory stages; globally serialized organization scheduling; accepting a terminal entity without its immutable certificate; treating a booking's physical UUID as a globally unique domain Command identity.

**Still unproven:** death between outbound publication/claim/ACK and domain Command commit; concurrent conflicting workers with stale fencing during OS death; complete O2C→Payments→R2R plus MRO causal graphs; PostgreSQL server termination/network partition; unavailable/recovering physical instances and capacity >1, mixed legacy plus authoritative pool ownership, fairness of recurrent scheduling under load, and external non-idempotent effects. Keep #406 open. Further experiments must add real fatal cut locations and compare canonical history, not merely endpoint success.

## Gate

Run the existing PostgreSQL integration job (which includes the full federated test file), Python 3.12–3.14, coverage floor 95%, benchmark/chaos/distribution and frozen Manufacturing/O2C/MRO replays. Upload the required multi-window artifact or fail the PostgreSQL gate. Merge only when exact-head checks and reviews are clear.

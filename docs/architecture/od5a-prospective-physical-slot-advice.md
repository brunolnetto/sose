# OD-5a — bounded prospective physical-slot advice (#477)

**Status: read-only PostgreSQL physical availability oracle, not scheduling
authority, waiting admission, or five-arm certification.**

After #527, a blocked owner retains its deterministic trigger and a typed
durable `od5a_shared_capacity_wait` record. This is correct under
historical outage/occupancy: the original interval cannot suddenly become
available merely because another owner finishes or equipment recovers later.
Automatically granting that *same old interval* would rewrite physical truth.

The physical ledger's `suggest_next_start(request, not_before, latest_start)`
is now a **bounded advisory query** over authoritative persisted
`TemporalReservation` and `TemporalOutage` intervals. It:

- requires the original immutable request identity, exact registered pool and
  physical instance, positive service duration, and timezone-aware search
  interval with `not_before >= request.start_at`;
- considers actual `effective_end` on booking/release/preemption and outage
  recovery; a pool outage blocks all its instances, but an instance outage
  does not block an independent instance;
- proposes the earliest feasible start within the caller's explicit upper
  bound; occupancy can only improve at recorded interval ends, so it checks
  the lower bound and all relevant durable end boundaries;
- applies the existing exact capacity overlap predicate for fungible
  capacity > 1 and for indivisible named instances; no preemption is inferred;
- refuses an already-booked reservation identity, never emits resource events,
  never mutates source/job/trigger and never promises a grant.

The advisory **method** deliberately does not acquire a PostgreSQL
advisory lock. An already-constructed `PostgresTemporalResourceLedger` can
be queried inside an outer UoW without retaining a new pool lock.
**Construction is distinct:** `PostgresPersistence.temporal_resources()`
still bootstraps tables with a namespace-scoped DDL advisory lock; construct
both ledger handles *before* beginning an intentionally long-lived SQL UoW.
Removing/bootstrap-caching that preexisting DDL guard requires its own
conformance and remains within the broader #477 hardening scope.
Read consistency is deliberately
weaker than booking authority: another owner can change capacity even while
the suggestion is being computed. SQL filters bookings and outages to
`[not_before, latest_start + requested_duration)` using native GiST
`tstzrange(start_at, effective_end, '[)') && requested_horizon` predicates;
these constrain **both** physical time bounds and treat zero-length recovered
outages as empty. No `btree_gist` extension is required. Independent
future-heavy PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` tests demand that the
GiST indexes actually prune distant future intervals, not merely appear in
a query plan. Pool selectivity is independently indexed: bookings have a
pool-key-leading B-tree, and outages now have an explicit `pool_key` B-tree
that PostgreSQL can use or combine with the temporal GiST. A second real
`EXPLAIN ANALYZE` fixture probes 1,500 simultaneous foreign-pool outages
and requires the pool-key index instead of scanning unrelated pools.
All test-only synthetic rows roll back before any canonical
ledger audit; exact reservation-ID uniqueness is still checked.
It is **not** a durable reservation or a linearizable promise. A different
owner may book that same suggested interval before the source can be legally
rescheduled; the eventual real `reserve` must atomically deny the conflict.
Real PostgreSQL
falsifiers prove that race, physical outage/recovery, half-open intervals,
fungible capacity boundaries, no retroactive start and zero ledger writes
during advice, including independent reconnect.

## Read-only bridge from a durable shared wait (#532)

`suggest_od5a_shared_wait_slots(store, job_id, shared_pool_binding,
not_before, latest_start)` reads an existing `od5a_shared_capacity_wait`
and returns typed per-instance hints, never a physical grant, source
`ScheduledWork` mutation or a jointly admissible reschedule.

The query uses an explicit non-bootstrapping physical ledger reader:
`PostgresPersistence.temporal_resources(bootstrap=False)`. This requires an
already initialized namespace but does **not** create tables or indexes, and
does not take a namespace-wide DDL advisory lock for unrelated organizations.
The read path still uses the source's own organizational lock to obtain a
stable snapshot; it does not acquire a pool-wide booking lock.

The durable wait now records `wait_schema_version=2` and a canonical
SHA-256 digest of the full preflight source attributes, in addition to the
previous exact action, trigger, pool-binding fingerprint, epochs and grant IDs.
A second independent durable witness, `physical_wait_id`, is placed on
the claimed runner lease *in the same SQL transaction* that first creates the
waiting entity. It is carried unchanged across fenced recovery epochs.
Advice, retry, and physical source commit verify the bidirectional link;
deleting either side causes fail-closed refusal, not an apparent first
attempt. A successful committed action atomically marks the wait satisfied
and removes the lease marker before the lease is released.
The read-side preview validates that digest and the complete claimed-lease
lifecycle before reconstructing exact service intervals and performing
individual `suggest_next_start` calls.

**Compatibility and tampering boundary:** schema-less (pre-v2) waits
and v2 waits missing `source_digest` are **not authorized** for either
prospective advice or a physical source retry. If the runner accepted a v2
wait after those fields were removed, a corrupted record could masquerade
as the old schema and bypass provenance checking. Existing pre-v2 waits must
be quarantined for independent reconciliation; there is no inferred historic
digest, automatic migration, or silent physical execution. This explicitly
tightens the earlier compatibility rule to fail closed.

Hints may become stale immediately, may not jointly fit if multiple grants
share capacity, and never permit execution. All eventual rescheduling must be
versioned, preserve immutable exogenous demand and original causal identity,
charge wait/intervention costs, and obtain fresh grants in one fenced commit.

## Not yet implemented

This PR deliberately does NOT rewrite the OD-5a synthetic arrival/service
`StateChart` due time or assert that a rejected old source event becomes
legally executable at the suggested later time. Implementing that requires
a **separate versioned rescheduling action** with an original-trace identity,
immutable exogenous demand, explicit wait cost, causal parent, RNG/CRN
equivalence, epoch fencing, and policy on commitment after a stale advisory.
Fairness, starvation, preemption, service interruption, SimPy three-way
conformance and crash/restart at the rebooking COMMIT are still #477 gates.

OD-5a remains `NOT_ADMITTED`. No 17,920 confirmatory runs are authorized.

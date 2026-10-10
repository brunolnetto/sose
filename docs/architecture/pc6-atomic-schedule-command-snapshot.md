# Atomic ScheduledWork / Command snapshot — PRD / TRD / ADR

## PRD
Stop classifying concurrent consumption/cancellation of durable schedules as permanent corruption. In the observed real PC6 multi-worker test, a worker raised "scheduled work references missing command" while another worker could mutate the scheduler concurrently. Detect genuine orphan records without split-read false positives. Frozen historical v1 semantics remain untouched.

## TRD
ScheduledWork + Command is one durable record pair. Existing DurableScheduler.pending() and cancel() read them via separate persistence methods, each refreshing the current PostgreSQL state independently; another worker can commit deletion between them. Expose read-only scheduled_work() enumeration in UnitOfWork and read both records from one transactional snapshot. Cancel performs read, invariant validation and both deletes in one transaction. A missing work record yields False (idempotent); a work record without its Command *in the same snapshot* is an error. Add independent regression tests forbidding cross-transaction reads and fault-injecting a real orphan. Existing real PostgreSQL multi-customer statechart remains an immutable hard test.

## SQLite nested-transaction compatibility
SQLite's active BEGIN IMMEDIATE transaction already provides the read scope. When called during an existing SQLite transaction, pending() uses that connection's current in-transaction view rather than opening a nested BEGIN (unsupported). Two SQLite adapter regressions guard this case. PostgreSQL and other adapters use the UnitOfWork pair snapshot.

## ADR
Do not suppress RuntimeError or indiscriminately retry a corrupted snapshot. Remove the TOCTOU window and retain corruption detection. PostgreSQL's shared advisory transaction lock and record-level concurrency may still admit conflicting schedules at a lower level; a true cross-worker write serializability and multi-interleaving stress gate is separate evidence. A green test does NOT prove externally exactly-once effects or close #406.

## Gates
Python 3.12–3.14, PostgreSQL real concurrency, coverage >=95%, chaos, benchmarks, historical v1 replay. No changes to frozen organizational artifacts.

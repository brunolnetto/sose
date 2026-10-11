# OD-5a — real OS-process recovery falsifiers (non-confirmatory)

Tracking: #477; prerequisite reference reducer: #474, and immutable physical mechanism pilot: merged #473/#476.

## PRD

Can multiple **real** operating-system worker processes rebuild their independent SQLite connections and progress a fixed OD-5a reference workload without duplicate semantic effects, missing causal predecessors, or global organizational clocks?

## TRD / evidence

- Same-job race: launch two separate `spawn` processes behind a shared start gate, both repeatedly calling the same authoritative `advance_od5a_durable` transaction. For all five registered arms, compare the **full** durable entity and immutable `DomainEvent` sequence against uninterrupted execution in a different SQLite file. Duplicated stage completions and unpaired leases fail.
- Multi-organization race: three independent organizational job IDs and treatment arms execute concurrently against one SQLite store, each with its own `Entity` clock and per-job physical resource definitions. Compare **every per-job** entity/event history to serial control; complete all jobs with no active physical reservation and preserve job-scoped correlations.
- Actors are separate OS processes, not separate in-memory services or threads; each opens and closes its own persistence connection. No SimPy environments or ephemeral resource objects are persisted.
- Use small hand-specified exogenous demands with microsecond-exact fractional intervals and colon-bearing IDs. No confirmatory design runs or results produced.

## Limits / ADR

SQLite **naturally serializes writes at the DB transaction boundary**; these tests demonstrate atomic multi-process job progress, not parallel commit throughput, explicit job-lease/fencing semantics, PostgreSQL concurrency or a production recurring runner. They do **not** claim the full #477 gate passed. Follow-up work must validate real publication/claim/ACK/Command/effect fencing and faults, physical instance recovery, multi-domain fanout, and a separately witnessed causal-equivalence bundle before any independent execution-freeze seal.

Historical v1 freezes, v1 scientific findings and PC6 recovery certification remain immutable.


## P1 concurrency falsification hardening

Each spawned OS worker must establish its own SQLite connection and reach a **shared readiness barrier** before entering its first authoritative transaction. A second barrier prevents any process from draining the workload until *all* participants have completed at least one distinct committed advancement. Each process reports its own first-step result through an interprocess queue; missing, zero, or duplicated attestations fail the test. This proves real multi-process participation and a concurrent start intent instead of accepting a late observer as a successful contender. The SQLite backend still serializes actual write transactions: this test does not claim simultaneous commits or replace production fencing. Complete per-job event and state equivalence remain the final oracle.

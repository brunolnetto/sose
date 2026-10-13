# Multi-organization causal certification protocol (OD-5a #490)

**PRD/TRD/ADR — evidence protocol, not production promotion.**
Owners: SOSE domain implementers, persistence reviewers, scientific protocol
reviewers. First candidate: OD-5a integrated source-to-boundary runner.
Status: **FALSIFICATION IN PROGRESS**. OD-5a scientific freeze **NOT_ADMITTED**.

## Purpose and bounded decision

SOSE must run multiple organizations with individually committed clocks, finite
resource ledgers, scoped ownership, external boundary deliveries, and durable
effects. Infrastructure has to be reusable by Manufacturing, O2C, MRO,
Trading Company/PC6, and the intended wider organizational catalog, **without
assuming domain semantics are interchangeable**.

This certificate is a black-box comparison of durable facts and failure
boundaries. It deliberately does not introduce a generic organization runner,
change SimPy, implement a global coordinator, certify native physical
SimPy arbitration, or declare five-arm scientific admissibility.

## Certified unit of comparison

`tests/support/causal_certificate.py` captures a **full-value** projection:

- authoritative source entities (including clocks and causal state) and all
  matching **or organization-correlated** DomainEvents (including wrong-key source
  entity identities, full DAG and tick);
- persisted definitions and occupied resource reservations;
- **all** typed boundary messages enumerated independently of deliveries (including
  orphan publications), all scoped deliveries, **nullable** delivery-linked ACK
  consumptions and **every** scoped receipt (including mismatched delivery IDs);
- **nullable** expected staged Commands **plus every organization-correlated pending Command**
  (including wrong-key extra executable intents), **nullable** event-keyed
  observations and **every** correlated observation entity, and
  **nullable** immutable BusinessEffectApplied certificates.

Missing evidence remains explicit `None`; totals and terminal throughput are
not adequate substitutes. Every organization-correlated BusinessEffectApplied
is also enumerated independently of expected message IDs: a duplicate or
mis-keyed receipt must falsify equivalence rather than vanish from the view. The caller supplies the domain's entity types,
physical event names, command identity namespace and organization identities.
Job checkpoints are separately required because the independent reducer
oracle does not own a recurring SimulationJobState; for every committed
OD-5a source step: `run_count == next_tick == source.step`,
`logical_time == source.updated_at`. Terminal probes must not create steps.

A domain implements its **own** oracle and physical truth requirements, then
reuses this transport/recovery evidence protocol. Different domains may
require inventory versions, fiscal legs, financial postings, resource
preemption, contracts, or causal effects that are **additional** mandatory
facts; no missing domain evidence can be labeled certified by this projection.

## Three executable falsification gates

### G1: Real OS death around durable phase boundaries

Spawn a distinct OS worker. Kill it **inside the staging UoW, after
the phase-specific write but before COMMIT**, for the first occurrence of
`publish`, `claim`, `ACK/Command`, `materialize`, `certify`; a simple
pre-invocation death does not qualify. Kill again after the respective
independent COMMIT but before worker response.
Verify every intervening persisted snapshot against #488 (no synthetic event
or duplicate effect). When a claimed owner dies, reclaim using a **new**
owner after expiry and advance under the new epoch. Assert the complete
source DAG, physical reservations and downstream receipts after each crash.

SQLite supplies fast complete phase coverage; source-lease recovery on live
PostgreSQL remains separately tested. The PostgreSQL engine's server-trusted
lease clock is never overridden by a worker clock.

### G2: Real PostgreSQL multi-process isolation

Spawn separate process/connection per organization, with one shared namespace.
Assert bounded physical and boundary progress can interleave without global
clock or namespace-exclusive boundary lock. A barrier **inside** two live
scoped `boundary_transaction` critical sections falsifies accidental global
serialization. Assert event/message correlation identity and exclusion of
foreign-domain deliveries.

### G3: Independent causal equivalence on every commit

Create two independent PostgreSQL namespaces: standalone #488 reducer plus
reconciler, and integrated recurring native-SimPy-dispatched adapter.
Run each organization through a separate persistent OS worker connection.
Release **one** worker commit at a time for this oracle gate, snapshot after
that individual COMMIT, then permit the next organization; otherwise two
simultaneous commits could hide a transient incorrect intermediate state.
G2 independently proves truly concurrent PostgreSQL critical sections.
On **every** bounded occurrence compare G3's full durable certificate,
and verify each integrated job's local authoritative tick/time. At completion
verify every `published / claimed / acknowledged / materialized / certified`
phase occurred and reservations were released.

## Fail/recover, not infinite hardening

A reproducible mismatch is a falsification. Open one minimal correction PR
with RED reproduction, GREEN fix, a passing full CI and independent review;
preserve original causal artifacts. Otherwise, stop changing production
semantics. There must be no blanket exception, coverage exclusion, automatic
retry hiding duplicates, or silent normalization of missing receipts.

Gate outcome is one of `PASS`, `FAIL`, `NOT_RUN`, `UNSUPPORTED`.
A passing pipeline without live PostgreSQL is `NOT_RUN` for G2/G3.
The explicit PostgreSQL job must run with `SOSE_TEST_POSTGRES_DSN`.

### Remaining evidence outside this bounded increment

- Deliberate kill of the **database server process** during an OD-5a
  boundary COMMIT, with reconnection/recovery comparison; these tests
  restart client connections but do not claim a database-server reboot.
- Native SimPy finite-resource arbitration in place of the durable
  reference reducer, including preemption and physical instance identity.
- Scientific OD-5a confirmatory five-arm freeze and the 17,920-run protocol.

These are separately tracked gates; they do not re-open the initial
source→boundary integration or license a scientific `ADMITTED` status.

## Domain admission checklist

| Domain/organization | Independent oracle required | Additional durable facts | Status here |
| --- | --- | --- | --- |
| OD-5a Organizational Dynamics | #488 standalone reducer/reconciler | Physical DAG and causal certification | Testing |
| Manufacturing | Manufacturing scientific freeze | Capacity, lots, WIP, preemption as applicable | Not assessed |
| O2C | O2C frozen reference | Order/invoice/settlement and decision trail | Not assessed |
| MRO | MRO frozen reference | Inventory reservations, work orders, purchasing, fiscal postings | Not assessed |
| Trading Company PC6 | PC6 frozen terminal history | Multi-domain Commands and financial/physical settlement | Not assessed |
| Insurance, Credit, Telecommunications, Aviation/Airports, Money | Distinct domain-specific baseline | Contracts, monetary identity, resource or regulatory artifacts as appropriate | Not assessed |

**ADR:** retain per-organization scoped PostgreSQL critical sections,
short separate causal commits and ephemeral native SimPy; centralize only
falsification shape and evidence. Wait for at least two actual domain
implementations to demonstrate a duplicated orchestration rule before
abstracting that rule into production. Preserve independent domain clocks,
fencing ownership, immutable event identities and replay audit across all
future organizations.

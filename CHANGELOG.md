# Changelog

## Unreleased

### Removed (deprecated in 0.13.0)

- Composition recovery under the namespace-wide writer lease, deprecated in 0.13.0
  (#635). `TradingCustomerRecoveryRunner(scoped_writer=False)` raises `TypeError`; the
  runner always uses its job-scoped lease and needs one `correlation_id`.
  `sose composition-recovery` always runs scoped, and `--scoped-writer` is accepted but
  has no effect. A job created under the global lease is refused until it is re-bound once
  with `--rebind-scoped` (`rebind_recovery_job_to_scope`), which stays available. Before
  upgrading, let any 0.13 occurrence in flight finish on 0.13: 0.14 cannot resume it, and
  the re-binding refuses an in-flight job.

## 0.13.0 — 2026-11-21

Release note: `docs/releases/v0.13.0.md`. Scoped writer leases on every backend, the
migration of recovery jobs off the namespace-wide lease, and that lease's deprecation
(removal in 0.14.0).

### Added

- `MemoryPersistence` implements writer fencing: the namespace-wide writer, job-scoped
  writers and `transaction(owner_epoch=, writer_scope=)`, with the SQLite/PostgreSQL
  contract; the scoped-writer conformance suite runs on memory, SQLite and PostgreSQL.
  `PersistentJobRunner` now works on memory. Memory leases are process-local and are
  not treated as durable fencing (`durable_writer_fencing = False`), so recovery and
  OD-5a still refuse a plain memory store (#637, #665).
- `sose composition-recovery --rebind-scoped` (and
  `sose.composition.recovery_rebind.rebind_recovery_job_to_scope`) moves an existing
  recovery job from the namespace-wide writer lease to its job-scoped lease, keeping
  its job id and history; `--scoped-writer` runs with the scoped lease. The global
  lease stays the default in 0.13 but is deprecated (see Deprecated) (#637, #666).

### Changed

- The example domains draw from the counter RNG (`CounterRandomSource`), keyed by
  logical identity. manufacturing, mro, order_to_cash and warehouse_fulfillment stay
  on the legacy RNG because they feed frozen evidence; legacy streams are unchanged.
  A database of a migrated example written by 0.12 and resumed now draws new
  decisions from the counter stream (#631, #668).

### Deprecated

- Composition recovery under the namespace-wide writer lease, deprecated in 0.13.0 and
  removed in 0.14.0 (#635). `TradingCustomerRecoveryRunner` without
  `scoped_writer=True` emits a `DeprecationWarning`, and
  `sose composition-recovery` without `--scoped-writer` prints the notice on stderr.
  Move an existing job once with `--rebind-scoped` (or
  `rebind_recovery_job_to_scope`), then run it with `--scoped-writer`. One scoped runner
  handles one `correlation_id`. Re-bind while no occurrence is in flight: 0.14 cannot
  resume an occurrence started under the namespace-wide lease (#672).

### Removed

- Incidental re-exports with no users and a canonical home elsewhere (#632, #667,
  #669): the `sose.backends.simpy` request/store/resource types (use
  `sose.backends.base`) and simpy's own `Container`/`Store`/`FilterStore`/
  `PriorityStore` classes (import them from `simpy`), `loads`/`MemoryPersistence`/`changes_for_dirty_records`
  from `sose.persistence.postgres`, `build_storage_plan` from `sose.cli`, and
  framework classes re-exported by `sose.examples.*.simulation` (including
  `RandomSource`). Modules that keep re-exports now declare them in `__all__`.

## 0.12.0 — 2026-11-20

Release note: `docs/releases/v0.12.0.md`. Completes the deprecation cycle
announced in 0.11.0 and adds owner-qualified organizational boundary routing.

### Added

- Owner-qualified organizational boundary routing (P496-A): v2 boundary messages
  bound to an admitted organization and producer/consumer component and owner,
  routed through `OrganizationBoundaryService`. Generic `BoundaryService` paths
  refuse or skip v2 messages; v1 message ids and codec bytes are unchanged
  (#496, #603).

### Removed (deprecated in 0.11.0)

- `ScenarioRule` (the `sose.scenarios.rules` module), `Engine(rules_for=...)` and
  `Engine.evaluate_entity`. Model external interventions with `Scenario` and
  `ScenarioEngine` (#634).
- The `sose.organizational.od2_legacy_audit` module (#634).
- The OD-2/3/4 module paths renamed in 0.11.0: `od2_actor_time_ledger`,
  `od3_decisions`, `od3_queue_runtime`, `od3_queue_ledger` and
  `od4_command_lineage` are gone; import from `actor_time_ledger`,
  `queue_decisions`, `queue_policy_runtime`, `queue_policy_ledger` and
  `work_command_lineage`. `od2_actor` and `od4_intake` remain only as the
  persisted class paths of `OrganizationalActor` and `ManagementWork`; import
  everything else from `actor_lifecycle` and `work_intake` (#629).

### Changed

- `make_random_source` defaults to `RNGMode.COUNTER` (order-independent draws).
  Pass `mode=RNGMode.LEGACY` to reproduce streams recorded with the legacy RNG;
  `RandomSource` itself is unchanged (#631).
- `BoundaryMessage` rejects a non-integer `contract_version` (bool/float) with
  "contract_version must be a positive integer (not bool/float)"; no in-tree
  producer ever wrote one (#603).
- CI runs PostgreSQL 19 (#658).

## 0.11.0 — 2026-11-20

Release note: `docs/releases/v0.11.0.md`. Completes the extended #634 removal,
announces the next deprecation cycle (removed in 0.12.0) and hardens the
organizational and transition-authority guards.

### Removed (deprecated in 0.9.0)

- Policy-less `TradingCustomerRecoveryRunner`: it raises `TypeError` without
  `resource_policies`. `sose composition-recovery` requires `--postgres-dsn` and
  `--resource-policies`; `--sqlite` is gone. Composition recovery is
  PostgreSQL-backed (#634, #659; tests ported in #656, #657).

### Deprecated (removed in 0.12.0)

- `ScenarioRule`, `Engine(rules_for=...)` and `Engine.evaluate_entity`; use
  `Scenario`/`ScenarioEngine` (#634, #654).
- The `sose.organizational.od2_legacy_audit` module (#634, #654).
- `make_random_source(...)` without an explicit `mode`; the implicit default
  becomes `RNGMode.COUNTER` in 0.12.0 (#631, #654).
- The OD-2/3/4 module paths, renamed to domain names: `od2_actor` →
  `actor_lifecycle`, `od2_actor_time_ledger` → `actor_time_ledger`,
  `od3_decisions` → `queue_decisions`, `od3_queue_runtime` →
  `queue_policy_runtime`, `od3_queue_ledger` → `queue_policy_ledger`,
  `od4_intake` → `work_intake`, `od4_command_lineage` → `work_command_lineage`
  (#629, #655). The persisted class paths
  `sose.organizational.od2_actor:OrganizationalActor` and
  `sose.organizational.od4_intake:ManagementWork` are **kept permanently** so
  existing data decodes.

### Security and integrity

- Opt-in `TransitionAuthority`: chained, owner-independent attested receipts
  with an attributes digest, a constant number of lookups per dispatch, and
  fail-closed handling of rollback and cross-owner forks (#545; #580, #602).
- OD-2 tracked actors: incremental actor-time ledger (#462, #587).
- OD-3 queues: owner-scoped durable ledger with seed anchor, items and
  applied-observation digests, and per-version slots against rollback; queues
  created by earlier versions are adopted on their first application (#463, #588).
- OD-4 intake: each new intervention re-verifies the original Command of every
  earlier paid decision, anchored to the work flow (#464, #589).
- OD-5a #477 bundles: detached RSA-SHA256 attestation verifier, RSA ≥2048,
  RSA-PSS refused (#585).

### Tests and tooling

- Coverage back above the 95% floor (95.5% on main): #591–#597, #599 (#590).
- Benchmarks: persistence, engine and temporal-backend tranches (#610; #611, #612).
- The release upgrade gate now starts from `v0.10.0`.

## 0.10.0 — 2026-11-19

Release note: `docs/releases/v0.10.0.md`. This release completes the ADR-0011
deprecation cycle started in 0.9.0. One window was extended instead.

### Removed (deprecated in 0.9.0)

- Names moved by the quality-ratchet splits can no longer be imported from the
  module they were split out of. Import them from the module that defines them,
  which each 0.9 warning named (#632, #649).
- `PersistentJobRunner` requires job-scoped writers: `scoped_writer_epoch`,
  `claim_scoped_writer` and `transaction(writer_scope=...)`. Persistence without
  them raises `TypeError`. SQLite and PostgreSQL provide them (#637, #650).
- Publishing a new boundary message that has a `causation_id` but no
  `causation_kind` raises `ValueError`. Untyped messages already durable stay
  readable and still satisfy re-derivation (#644, #652).

### Changed

- The #634 deprecation window is **extended to 0.11.0**: policy-less
  `TradingCustomerRecoveryRunner` and the SQLite path of `sose composition-recovery`.
  The roughly 75 recovery tests that use it are ported to PostgreSQL first.
  ADR-0011 gains an amendment allowing (never shortening) extensions (#651).
- The release upgrade gate works for any previous minor: it migrates unowned
  databases and opens owner-qualified ones directly. It now starts from `v0.9.0`.

## 0.9.0 — 2026-11-18

Release note and compatibility contract: `docs/releases/v0.9.0.md`.

### Breaking changes

- **Owner-qualified durable runtime only (ADR-0009).** Every live runtime is
  explicitly owned:
  - `Engine(..., owner_id=...)` is required;
  - the `Persistence` / `UnitOfWork` position, committed-tick and scenario-state
    methods take `owner_id=`;
  - `ScheduledWork`, `Command` and `DomainEvent` carry the owner;
  - the builtin domains' runtime factories (`build_runtime`, `seed_*`) take `owner_id`.
- **SOSE 0.8 SQLite databases are refused, not adopted.** Opening one raises
  `LegacyUnownedSnapshotError` and leaves the file untouched (SQLite snapshot codec
  2, #643). Migrate it offline into a new database with
  `sose migrate-owners --sqlite <old> --output <new> --owner <owner>` (#646). The
  release workflow proves this upgrade path from the real 0.8.0 wheel.

### Deprecated (removed in 0.10.0, ADR-0011)

- `TradingCustomerRecoveryRunner` without `resource_policies`, and with it the
  `--sqlite` path of `sose composition-recovery`. From 0.10, composition recovery
  requires PostgreSQL; use `--postgres-dsn` and `--resource-policies` (#634, #638).
- `PersistentJobRunner` on persistence without scoped writers, including subclasses
  whose `transaction()` lacks `writer_scope` (#637, #640).
- Importing a name from a module it was split out of during the quality-ratchet
  refactors. Each warning names the module to import from instead (#632, #642).
- Publishing a boundary message that has a `causation_id` but no `causation_kind`
  (#644, #647).

### Added

- `sose migrate-owners` / `sose.persistence.owner_migration` (#646):
  - offline, exclusive-lock owner migration of SOSE 0.8 SQLite databases;
  - keeps every identity, records a semantic digest and keeps a rollback copy;
  - verifies the result with the runtime diagnostics.
- Release upgrade gate (`scripts/check_release_upgrade.py`, `release.yml`):
  previous-minor wheel → migration → this release's runtime.
- Job-scoped writer leases on the SQLite backend, with the same contract as
  PostgreSQL. `PersistentJobRunner` claims a lease per job, so independent jobs in
  one namespace no longer fence each other out (#637, #639, #640).
- `sose composition-recovery --postgres-dsn/--namespace/--resource-policies` (#638).
- Deprecation policy and the `sose._deprecation` helpers (ADR-0011, #638).
- Codebase quality ratchet in CI (ADR-0010).
- Organizational composition and admission (ADR-0008), cross-domain boundary
  messages (ADR-0006), PC6 durable recovery, versioned scientific freezes (ADR-0007),
  and the OD-5a preregistered-experiment machinery.

- end-to-end recurring product contract covering domain configuration, persistent scheduler triggers, config revision, process reopen, and analytical sink delivery;

- read-only `sose plan` command resolving domain parameters, storage roles, runtime, and recurring job policy without opening external systems;

- recurring job policy CLI (`job show`, `job set-policy`) for declarative tick-batch configuration;

- CLI storage editing commands (`storage set-authoritative`, `storage add-sink`, `storage remove-sink`) with validate-before-write behavior;

- unified storage plan that classifies authoritative persistence and analytical sinks, validates required capabilities, and exposes a read-only `sose storage` inspection command;

- deterministic recurring-trigger identities derived from timezone-aware
  scheduler occurrence timestamps, exposed through Python and CLI;
- validated `sose config show` / `sose config set` commands for inspecting
  and editing declarative domain parameters without implicitly mutating durable jobs;
- domain-specific runtime controls across every builtin Reference, including
  pause/resume progression controls for otherwise fully automatic reconcilers;
- horizontal conformance that requires each builtin domain to expose at least
  one safe runtime-mutable operational parameter;
- runtime-vs-bootstrap parameter mutability metadata and enforcement for durable
  domain configuration revisions;
- optional Databricks and Snowflake analytical event sinks using idempotent SQL MERGE batches over the durable analytical outbox;

- durable analytical sink outbox with per-sink checkpoints, idempotent event batches, declarative sink configuration, and JSONL reference adapter;

- optional PostgreSQL remote persistence adapter using the shared incremental
  record/delta contract, with real-service CI conformance, restart, revision,
  and concurrent-writer tests;
- PostgreSQL-aware `sose init` scaffold using `SOSE_DATABASE_URL` rather
  than embedding credentials in sose.toml;
- bounded recurring job trigger batches with durable parent ownership, partial
  recovery, historical trigger idempotency, and configurable ticks per trigger;
- horizontal config contract requiring every builtin domain to expose
  domain-specific customizable parameters;
- persistence capability contract and declarative capability requirements for
  storage targets;
- CLI persistence metadata describing tested adapter guarantees;
- read-only `sose doctor` command for config drift, durable checkpoint,
  unresolved-trigger, and runtime consistency checks;
- end-to-end recurring-job acceptance flow from scaffolded MRO config through
  tick, inspect, reopen, and subsequent tick;
- recurring tick reconciliation across every builtin Reference plus the tutorial
  domain, with state-aware idempotent progression instead of end-to-end-only
  execution;
- recurring operational controls for Construction, Manufacturing, P2P,
  Record-to-Report, Transit, Aviation, Credit & Loans, Energy / Utilities,
  Insurance, and Order-to-Cash;
- recurring durable-job reconciliation for ITSM, Hospitals, Field Service,
  Hospitality, and Airports, including configurable capacities and temporal
  boundaries;
- recurring reconciliation hooks for Cards & Payments, Logistics, Telecom,
  Warehouse / Fulfillment, and Subscription / SaaS so durable jobs advance one
  tick per trigger instead of requiring end-to-end example execution;
- operational configuration knobs across Transit, Telecom, Aviation, Logistics,
  Hospitality, Field Service, Cards & Payments, Energy / Utilities,
  Subscription / SaaS, and Warehouse / Fulfillment, with executable evidence
  that overrides change durable truth;
- `sose apply --config sose.toml` for explicit idempotent application of
  edited domain configuration to an existing durable job;
- `sose init --domain <name>` scaffolding generated from each domain's actual
  validated defaults, with roundtrip coverage across the full builtin catalog;
- `sose` command-line interface for config validation, one-tick execution,
  durable inspection, domain discovery, and persistence-adapter discovery;
- declarative sose.toml job configuration with domain parameters, persistence
  adapter selection, runtime backend, and durable job identity;
- persistence adapter registry for memory, SQLite, incremental SQLite, JSONL,
  and optional DuckDB;
- durable recurring SimulationJob orchestration with one logical tick per external trigger, persisted config revisions, pause/resume, and crash-safe position recovery;

- structural copy-on-write UnitOfWork state forks that avoid deep-copying untouched durable records at transaction entry;

- incremental SQLite concurrency semantics covering independent readers,
  alternating writers, stale-cache refresh, lock contention, and conflicting
  Engine commands;
- persistence scaling curves at 10/100/1,000/10,000 durable entities for
  snapshot SQLite, incremental SQLite, JSONL, and DuckDB;
- UnitOfWork dirty-record tracking so record-oriented sinks encode only touched
  semantic identities instead of diffing the entire durable state;
- revision-aware incremental SQLite cache that avoids full record reloads when
  durable state has not changed;
- optional DuckDB persistence sink using the shared record/delta mapping;
- DuckDB benchmark comparison for scheduled execution and reopen/rebuild;
- incremental SQLite persistence using backend-neutral record deltas;
- append-only JSONL journal persistence using the same record-delta contract;
- benchmark comparison across snapshot SQLite, incremental SQLite, and JSONL;
- v0.9 adoption/operations roadmap, persistence adapter decision criteria, and
  explicit SOSE 1.0 readiness checklist;
- reproducible runtime benchmark harness with JSON output, CI smoke execution,
  and weekly/manual full benchmark workflow;
- read-only runtime diagnostics with durable counts, recovery position, and
  stable consistency issue codes;
- transactional SQLite schema migrations with independent schema/codec
  versioning and a tested v1 -> v2 path.

### Changed

- Composition flows publish typed causation (`causation_kind="boundary"`), so the
  causal audit sees no opaque links for current flows. New messages in those flows
  persist the discriminator. Messages recorded untyped are kept as written and
  still satisfy re-derivation (#644, #647).
- Structural refactors driven by the quality ratchet (#615–#628, #636) take the
  baseline from 244 to 24 violations without behaviour change. Moved names stay
  importable from their old modules, with a deprecation warning.
- Live single-instance and untyped modes are no longer labelled "legacy" in the
  code (#633, #645).

## 0.8.0 — 2026-09-28

### Added

- `sose.api` as the compatibility-relevant domain-author facade;
- SQLitePersistence as the first external transactional persistence adapter;
- tagged-JSON durable serialization without pickle;
- built wheel/source-distribution validation in CI;
- release-candidate workflow with tag/version/release-note checks;
- minimal restart-safe getting-started domain and tutorial;
- repeated-restart helper and semantic hardening stress tests;
- Reference conformance and horizontal anti-drift gates across 21 promoted domains;
- Telecommunications, Energy / Utilities, Public Transit / Rail, Field Service,
  Hospitality, Warehouse / Fulfillment, and Subscription / SaaS References.

### Changed

- development direction shifts from domain-frontier expansion to library
  stabilization and adoption;
- public/advanced/compatibility/internal API tiers are now documented;
- release documentation separates chronological changes, milestone closure,
  compatibility policy, and planned gates;
- durable lifecycle patterns for scheduling, Store selection, resources, and
  restart testing were consolidated across References;
- monetary References now preserve currency provenance and enforce explicit
  minor-unit arithmetic where arithmetic actually occurs;
- float-facing minor-unit validation uses ULP-aware tolerance capped strictly
  below half a minor unit instead of an arbitrary magnitude cutoff.

### Fixed

- restart/race defects found through cross-domain review, including resource and
  ScheduledWork lifecycle duplication, Store selection ownership, overlapping
  incident restoration, captured usage replay, appointment expiry cleanup,
  inventory selection eligibility, and monetary odd-cent/zero-partial behavior.

### Compatibility

- new domain/integration code should prefer `sose.api`;
- `context.schedules` remains the durable scheduling contract;
- `context.scheduler` remains compatibility-only;
- concrete optional backends remain explicit imports;
- SQLite persisted dataclass/enum module paths are persistence compatibility
  concerns before 1.0.

### Architectural decisions

- no core Money primitive yet;
- no generic TemporalCapacity primitive yet;
- no generic QualifiedResource primitive yet;
- no generic inventory-ledger primitive yet.

The v0.8 milestone is complete when the release commit passes the Python
3.12/3.13/3.14 matrix, coverage, distribution build/install smoke, and release
candidate metadata validation.

## 0.7.0 — 2026-09-26

### Added

- durable FIFO, priority, and filter Store semantics;
- durable Store get completion results and terminal request identity;
- durable Container definitions, levels, intents, and terminal operation results;
- crash-consistent durable preemption with terminal preemption records;
- full v0.7 multi-restart runtime equivalence gate;
- deterministic crash-injection test utilities and recovery matrix;
- reusable persistence conformance suite;
- named runtime recovery participants and explicit recovery ordering;
- CI matrix for Python 3.12, 3.13, and 3.14 with an 80% branch-coverage gate;
- formal cross-domain implementation blueprints.

### Changed

- Runtime recovery validates the complete durable plan before reconstruction.
- Durable Container replay is ordered explicitly by semantic sequence.
- Container quantities reject non-finite values.
- Runtime reconstruction is coordinated through named participants instead of
  subsystem-specific RuntimeRebuilder arguments.
- README and durable-runtime architecture documentation now describe the v0.7
  boundary.

### Compatibility

- `context.schedules` is the durable scheduling API.
- `context.scheduler` remains a legacy in-memory compatibility bridge and
  should not be used by new domain examples.
- SimPy-native environments, processes, events, queues, callbacks, requests,
  and generators remain intentionally non-durable.

### Architectural boundary

```text
durable semantic truth
    ≠
backend-native execution state
```

The next milestone is domain implementation, beginning with operational examples
that exercise the complete durable runtime.

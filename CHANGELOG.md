# Changelog

## Unreleased

### Added

- bounded recurring job trigger batches with durable parent ownership, partial
  recovery, historical trigger idempotency, and configurable ticks per trigger;
- horizontal config contract requiring every builtin domain to expose
  domain-specific customizable parameters;
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

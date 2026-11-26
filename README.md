# SOSE — Synthetic Operational System Engine

[![CI](https://github.com/brunolnetto/sose/actions/workflows/ci.yml/badge.svg)](https://github.com/brunolnetto/sose/actions/workflows/ci.yml)

SOSE simulates operational systems; it does not generate independent rows. Each
domain is a set of persistent entities whose lifecycles are StateCharts. SOSE runs
them on a deterministic logical clock, with durable scheduling, finite resources and
external scenarios, and records every state change as a causal event. The output is
operational data whose rows explain each other: who did what, when, because of what,
and with which resource.

```text
current state + legal behavior + stochastic semantics + logical time
+ external scenarios + causal events  =  next operational state
```

It targets domains with lifecycle rules, exceptions, queues and time: maintenance
(MRO), order-to-cash, procure-to-pay, warehousing, logistics, manufacturing,
healthcare, aviation, insurance, payments, telecom and more.

## Status

- **Version 0.13.0**, Python 3.12+, MIT licensed. Releases and policy are in
  [`docs/releases/`](docs/releases/README.md). Deprecations follow
  [ADR-0011](docs/architecture/decisions/adr-0011-deprecation-policy.md).
- The durable runtime, the persistence adapters, the CLI and organization
  composition are tested on every pull request, including real PostgreSQL. Coverage
  must stay at or above 95%, and a quality ratchet must pass.
- **Known limitation: one case per run.** Every builtin domain runs a single
  configured reference case, with its happy or sad path chosen by configuration.
  `energy_utilities` is a partial exception: it records a recurring meter reading
  on every tick.
  This keeps exact ground truth and frozen scientific evidence, but it produces no
  volume or variation for population metrics. A seeded, opt-in **workload mode** is
  proposed in [ADR-0012](docs/architecture/decisions/adr-0012-seeded-domain-workload-generation.md)
  (issues #697–#703).
- Scientific studies (OD-5a and others) have their own admission gates. They are not
  admitted until their protocol is.

## Core ideas

| Layer | Owns |
|---|---|
| **StateChart** (`python-statemachine`) | Legal behavior: states, transitions, guards, compound and parallel states. |
| **Probabilistic transition graph** | Weights over the currently legal events. Guards are applied before normalization ([docs](docs/probabilistic-transition-graph.md)). |
| **Scenario engine** | External conditions and interventions that change context and weights, never lifecycle topology ([docs](docs/architecture/scenario-engine.md)). |
| **Scheduler and logical clock** | Temporal semantics. Domain code never reads the wall clock. |
| **Durable runtime** | Entities, commands, events, scheduled work, resources, stores, containers, preemption and the recovery position, all persisted. Execution backends such as SimPy are ephemeral and rebuilt from durable state ([docs](docs/architecture/durable-runtime.md)). |

> StateCharts decide what is legal. SOSE decides how likely each legal event is,
> samples it from a scoped, seeded stream, and records the result durably.

**Recurring jobs.** One external trigger advances exactly one logical tick and
commits a durable checkpoint, so cron, Airflow, Databricks or Kubernetes can drive a
simulation without a long-running process. Restart equivalence with a continuous
run is tested for the documented crash and fault windows
([docs](docs/architecture/recurring-domain-jobs.md)).

**Organizations.** Several domain components can be composed into one organization,
with owner-qualified jobs, cross-domain boundary messages and a canonical
organization snapshot. Boundary delivery is idempotent, and a consumer's handler and
its ACK commit in one transaction; exactly-once transport is not assumed
([ADR-0006](docs/architecture/decisions/adr-0006-cross-domain-boundary-messages.md)). The snapshot can include
physical pools admitted against a PostgreSQL resource ledger
([ADR-0008](docs/architecture/decisions/adr-0008-organization-composition-admission.md),
[ADR-0009](docs/architecture/decisions/adr-0009-owner-qualified-runtime-only.md)).

**Attested transitions (opt-in).** With a transition authority, the Engine signs each
StateChart transition. An attested job also signs attribute-only saves, through its
persistence wrapper. Receipts are chained per entity and use HMAC or Ed25519; with
Ed25519, verifiers hold only the public key. Within one store, a forged, unsigned,
reordered or rolled-back step fails verification. A chain is not bound to a store:
copied together with its entity into another store, it still verifies. This is an
opt-in signing root, not a general production security boundary
([docs](docs/architecture/engine-transition-attestation-545.md)).

## Domain catalog

`sose domains` lists the builtin domains, and `sose domains <name>` shows a domain's
parameters.

- **Industry references:** airports, aviation, cards_payments, construction,
  credit_loans, energy_utilities, field_service, hospitality, hospitals, insurance,
  itsm, logistics, manufacturing, mro, order_to_cash, p2p, record_to_report,
  subscription_saas, telecom, transit, warehouse_fulfillment, warehouse_management.
- **Canonical concurrency problems:** dining_philosophers, job_shop,
  producer_consumer, readers_writers, sleeping_barber.
- **Tutorial:** tutorial_job.

## Quickstart

Install from a clone, with the optional SimPy backend and the development tools:

```bash
git clone https://github.com/brunolnetto/sose && cd sose
python -m pip install -e ".[simpy,dev]"
```

Create, apply and advance a durable job:

```bash
sose init --domain mro                       # writes sose.toml from validated defaults
sose validate --config sose.toml
sose apply --config sose.toml                # creates or updates the durable job
sose run --config sose.toml --trigger-id run-001   # advances one logical tick
sose inspect --config sose.toml              # reads the checkpoint, changes nothing
```

To build your own domain, start with
[Build your first durable domain](docs/getting-started/first-domain.md). Then see
[recurring jobs](docs/getting-started/recurring-job.md),
[the CLI](docs/getting-started/cli.md) and
[storage configuration](docs/getting-started/storage-config.md).

## Persistence and analytical sinks

| Kind | Adapters |
|---|---|
| Authoritative persistence | `memory`, `sqlite`, `sqlite_incremental`, `postgres`, `duckdb`, `ducklake`, `clickhouse`, `jsonl` |
| Analytical sinks (asynchronous, idempotent) | `jsonl`, `databricks`, `snowflake` |

Optional extras: `simpy`, `postgres`, `duckdb`, `ducklake`, `clickhouse`,
`databricks`, `snowflake` and `attestation` (Ed25519). `sose persistence` and
`sose sinks` list each adapter's capabilities.

Operational persistence is the source of truth. A sink outage never rolls back
committed simulation progress
([ADR-0005](docs/architecture/decisions/adr-0005-canonical-experiment-data-product.md)).

## What the data contains

- **Entities:** the current state and attributes of each simulated object.
- **Events:** an immutable `entity.state_transition` per change, with `causation_id`
  (immediate cause) and `correlation_id` (business flow). This suits process mining.
- **Resources:** demands, reservations and release intents, plus stores, containers
  and preemption outcomes.
- **Jobs:** durable checkpoints and completed triggers.
- **Organizations:** boundary messages, deliveries and ACKs, and optionally
  transition receipts.

Ground truth is typed (analytical, mechanistic or invariant) rather than "whatever
the simulator produced"
([ADR-0003](docs/architecture/decisions/adr-0003-ground-truth-taxonomy.md)). How a
business metric catalog can be derived from this data, and what is still missing, is
in [metric-catalog-derivation.md](docs/architecture/metric-catalog-derivation.md).

## Kernel invariants

1. Scenarios never mutate entity state directly; StateCharts own lifecycle rules.
2. Scenarios change context and policies; they never duplicate lifecycle topology.
3. Probability is evaluated only over currently enabled events. Policies give
   weights, and normalization is a runtime concern.
4. Eventless StateChart transitions are never sampled by SOSE.
5. Every random draw comes from a scoped, seeded stream. Identities are
   deterministic.
6. Domain logic never reads the wall clock.
7. Every state change produces an immutable event. State and history are persisted
   separately.
8. A tick is committed only after its effects are durable. Backends are rebuilt from
   durable state.
9. Simulator-private bookkeeping is not part of the downstream data contract.

## Documentation

- **Getting started:** [`docs/getting-started/`](docs/getting-started/README.md)
- **Authoring API:** [probabilistic transitions and the decision
  API](docs/probabilistic-transition-graph.md) and
  [factories](docs/factories.md). Domain code schedules through
  `context.schedules`; the in-memory `context.scheduler` is a compatibility bridge
  only.
- **Architecture:** [`docs/architecture/`](docs/architecture/), including the
  [durable runtime](docs/architecture/durable-runtime.md),
  [persistence model](docs/architecture/persistent-model.md),
  [SimPy backend](docs/architecture/simpy-backend.md),
  [domain blueprints](docs/architecture/domain-implementation-blueprints.md) and
  [version 1 readiness](docs/architecture/version-1-readiness.md)
- **Decisions (ADRs):** [`docs/architecture/decisions/`](docs/architecture/decisions/)
- **Documentation policy:** [`docs/governance/documentation-policy.md`](docs/governance/documentation-policy.md)
- **Releases:** [`docs/releases/`](docs/releases/README.md)
- **Roadmap:** [`docs/ROADMAP.md`](docs/ROADMAP.md)

## Development

```bash
python -m pytest -q -m "not slow"     # the CI default
python -m pytest -q -m slow           # the long suites, run separately
```

PostgreSQL tests run when `SOSE_TEST_POSTGRES_DSN` is set. Lint is not a CI gate
today: `ruff` reports existing findings on `main`. A pull request merges with
green CI, at least 95% coverage, a passing quality ratchet
([ADR-0010](docs/architecture/decisions/adr-0010-codebase-quality-ratchet.md)) and
an independent review of its exact head. Consequential architectural changes need an
ADR (see the documentation policy).

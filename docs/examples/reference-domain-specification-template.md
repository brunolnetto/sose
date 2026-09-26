# Reference Domain Specification Template

This document defines the canonical human-readable specification format for SOSE
reference domains.

A reference-domain specification is the primary semantic contract. Code implements it;
tests provide executable evidence for it.

```text
specification
    ↓ defines
domain semantics
    ↓ implemented by
src/sose/examples/<domain>/
    ↓ verified by
tests/test_<domain>_*.py
```

## 1. Purpose and scope

Describe the real operational system before describing SOSE.

Include:

- what business/operational problem is modeled;
- what is intentionally inside the executable slice;
- what is explicitly outside scope.

## 2. Operational story

Describe the process in plain operational language that a domain expert can read
without opening code.

Explain:

- what initiates the process;
- what must happen before work may advance;
- what constitutes successful completion;
- the important abnormal conditions.

## 3. Domain entities

For each persistent entity document:

### <Entity>

**Responsibility**

What business fact/process responsibility this entity represents.

**Relevant attributes**

List only attributes that matter to the example.

**Owns**

Which durable semantic truth belongs to this entity.

**Does not own**

Related truths that belong to inventory, resources, scenarios, other entities, etc.

## 4. StateCharts

Document every executable entity StateChart.

For each entity include:

1. a readable state diagram;
2. a transition contract table.

Recommended table:

| Current state | Command | Preconditions | Next state | Durable evidence required |
|---|---|---|---|---|

State names and command names must match the implementation exactly.

## 5. Process specifications

StateCharts explain one entity at a time. Process specifications explain how entities
and durable primitives interact.

### 5.1 Happy path

Describe the canonical successful end-to-end process.

For each step explain:

- triggering business fact;
- required durable preconditions;
- effects committed before the next lifecycle claim;
- resulting state.

### 5.2 Sad paths

Each representative abnormal flow is a first-class mini-spec.

For every sad path document:

**Trigger**

What causes the abnormal branch.

**Expected behavior**

State/process sequence.

**Durable truth**

What must remain observable after a crash.

**Recovery / terminal behavior**

How the flow resumes, compensates, retries, reworks, or terminates.

A reference domain must not be promoted if only the happy path is executable.

## 6. Commands and domain events

Document commands as explicit business intents.

Recommended table:

| Command | Target | Meaning | Preconditions |
|---|---|---|---|

Explain emitted domain events and their causal/correlation semantics.

## 7. Invariants

Assign stable IDs.

Example:

```text
MFG-01
A ProductionOrder cannot enter setup unless machine and operator reservations exist.
```

Tests should reference invariant IDs where useful.

Invariants must cover at least:

- lifecycle legality;
- resource gating;
- exactly-once physical effects;
- quantity/capacity consistency;
- business-exception semantics;
- restart behavior.

## 8. Durable truth and ownership

Document where the authoritative state lives.

Recommended table:

| Concept | Durable owner | Why |
|---|---|---|

Also state what is explicitly ephemeral/reconstructible.

## 9. Restart semantics

Define the restart contract in domain language.

List meaningful crash boundaries such as:

- after durable intent but before backend completion;
- after resource acquisition but before lifecycle transition;
- during a blocked or exception state;
- after physical effect but before business-state claim.

Define semantic equivalence precisely.

## 10. Scenario specification

For each scenario document:

- trigger;
- activation frequency;
- duration;
- effects;
- domain interpretation;
- what the scenario does *not* directly mutate.

## 11. Example runs

Include compact narrative traces for:

1. nominal execution;
2. at least one resource/capacity failure;
3. at least one business exception/rework/rejection;
4. restart of a representative sad path.

## 12. Executable evidence

Map specification claims to implementation/tests.

Recommended table:

| Specification area | Implementation | Tests |
|---|---|---|

## Reference-grade promotion gate

A domain is reference-grade only when all of the following hold:

1. persistent entities and explicit StateCharts exist;
2. state and command terminology in docs exactly matches code;
3. one canonical happy path is executable;
4. representative sad paths are executable;
5. resource/capacity constraints gate lifecycle transitions where applicable;
6. physical/quantitative effects are durable before business claims;
7. scenarios are explicit and restart-safe where applicable;
8. happy-path restart equivalence is tested;
9. representative sad-path restart equivalence is tested;
10. durable semantic truth is sufficient to reconstruct backend execution;
11. the specification maps each important rule to executable evidence.

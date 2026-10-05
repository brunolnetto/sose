# Organizational Dynamics — Pull-Request Review Canonical

This is the first Phase 7 Organizational Dynamics canonical. It models **well-specified pull-request review work**, not software product development as a whole.

## Scope

The canonical represents:

1. pull request opens;
2. CI delay completes;
3. work waits for finite reviewer capacity;
4. a reviewer performs an active review;
5. the review either passes or requests rework;
6. rework consumes author actor-time;
7. the item returns to the review queue until it passes.

Review dispatch is FCFS. Reviewer service requirements, revision requirements, and review outcomes are counter-RNG keyed by pull-request identity and logical mechanism. Changing reviewer capacity therefore changes queueing while preserving unaffected stochastic requirements for CRN comparisons.

## Measurement semantics

- CI is item `PROCESSING`; it does not invent a human actor interval.
- waiting for reviewer capacity is item `QUEUE`; it consumes no reviewer actor-time.
- active review is item `COORDINATION` and reviewer `COORDINATION` actor-time.
- requested changes are item `REWORK` and author `EXECUTION` actor-time.
- item ledgers remain contiguous from open to completion.

## Evidence classes

The default `ModelSpec` deliberately distinguishes external item-flow evidence from unobserved human effort:

- reviewer count — Observed when repository/team configuration supports it;
- CI duration — Observed from CI logs;
- mean reviewer service effort — Assumed;
- mean revision effort — Assumed;
- rework probability — Inferable from an explicit review-cycle definition.

Observed time-to-review should not be mislabeled as reviewer service effort.

## Outcomes

The canonical exposes only unit-bearing flow observables:

- lead time;
- throughput over the simulated observation interval;
- peak WIP;
- queue time;
- review cycles;
- item and actor ledger projections.

It intentionally defines no productivity or organizational-efficiency composite index.

## Limits Statement

This canonical:

- is A0 only;
- does not model discovery-driven work, scope change, item splitting/merging, or abandonment;
- does not model meetings, context switching, calendars, pressure, trust, incentives, or A3 agency;
- treats CI as a configured delay rather than a finite runner queue;
- treats review quality as a rework probability rather than an information-state model;
- does not infer human utilization from event timestamp gaps;
- uses a per-item author for rework and therefore does not yet model author-capacity contention.

Absence from these ledgers means absence from this model, not absence from real software organizations.

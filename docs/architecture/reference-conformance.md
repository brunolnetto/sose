# Reference Conformance

## Purpose

A domain marked **Reference implementation** must carry explicit evidence rather
than rely on prose status alone.

SOSE therefore maintains a repository-level reference catalog in:

`tests/support/reference_catalog.py`

and validates it through:

`sose.testing.conformance`.

## What the suite verifies

Every reference declares:

- its human-readable domain name;
- Python package;
- documentation directory;
- supported runtime capabilities;
- concrete evidence files for every declared capability.

The baseline required of every Reference implementation is:

- StateChart evidence;
- canonical happy-path evidence;
- representative sad-path evidence;
- restart-equivalence evidence.

Additional capabilities are declared only when relevant, for example:

- scenarios;
- resources;
- ScheduledWork;
- Store selection;
- preemption;
- crash recovery;
- illegal-prerequisite checks;
- probabilistic transitions;
- immutable occurrence evidence.

The conformance checker verifies structural claims only:

- evidence files exist and contain executable tests;
- baseline capabilities are present;
- declared capabilities have evidence;
- documentation exists and declares reference-implementation status;
- package/entities/statecharts/runtime modules import;
- runtime exposes the entrypoints declared by its contract (modern references
  normally use `build_runtime()` + `seed_reference()`; legacy references may
  declare their established equivalent);
- scenario modules import when scenario evidence is declared;
- catalog identities do not collide.

## What it deliberately does not infer

The checker does not decide whether a specific business invariant is correct.
It does not infer a happy path by inspecting function names, declare a resource
safe because a Resource exists, or replace domain-specific assertions.

A manifest entry is an explicit claim, and its listed tests are the executable
evidence supporting that claim.

## Promotion workflow

A domain moves from Blueprint/Partial to Reference only after:

1. its domain specification is complete;
2. its ReferenceContract is added;
3. its evidence files exist;
4. the reference-conformance suite passes;
5. the normal CI matrix passes.

This makes promotion status mechanically checkable while preserving the rule:

> core/test infrastructure may validate evidence structure; domain tests own
> semantic truth.

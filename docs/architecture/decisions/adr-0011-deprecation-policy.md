# ADR-0011 — Deprecation policy: warn now, remove in the next minor

- **Status:** Accepted
- **Date:** 2026-11-18
- **Decision owners:** SOSE maintainers
- **Related issues:** #634 (first deprecations), #635 (legacy inventory), #632, #637
- **Supersedes:** none
- **Superseded by:** none

## Context

SOSE has accumulated surfaces documented as "legacy" in code comments. Examples are the
policy-less trading-company recovery mode, the `ScenarioRule` / `Engine(rules_for=…)` hook,
and the re-export shims left by the quality-ratchet splits. None of them warned at run
time, so callers had no signal and no date. The repository had no `DeprecationWarning`
and no stated removal horizon.

SOSE is pre-1.0 (`0.8.x`). Users run it embedded in their own code, the CLI and durable
workers. Removing a surface with no warning breaks them silently.

## Decision

1. **Every deprecation emits a `DeprecationWarning` through `sose._deprecation.warn_deprecated`.**
   It is the single helper, so the message format stays uniform. The message names:
   - the deprecated surface;
   - `since`, the release that deprecated it;
   - `removed_in`, the release that removes it;
   - the replacement (`instead`).
2. **Removal happens in the next minor release** after the deprecation ships. A surface
   deprecated in `0.9.0` is removed in `0.10.0`. Patch releases never remove a deprecated
   surface, and per `docs/releases/release-policy.md` they cannot introduce one either (a
   deprecation is an API change, so it ships in a minor release).
3. **The warning points at the caller.** `stacklevel` is set so the warning is attributed
   to the user's call site, not to SOSE internals. A unit test asserts the attribution.
4. **The test suite stays warning-clean.** Suites that deliberately keep exercising a
   deprecated surface until its removal add a narrowly scoped `filterwarnings` ignore in
   `pyproject.toml` that matches the exact message prefix, with a comment naming the issue.
   A dedicated test asserts the warning itself. The ignore and the deprecated code are
   removed in the same change that performs the removal.
5. **Persisted data is out of scope for this policy.** Read paths for older durable
   formats (#633) follow their own migration plan. Identifiers that reach the codec or
   `deterministic_id` are never renamed by a deprecation.

## Consequences

- Each deprecated surface carries an explicit removal release. Release preparation for
  `0.10.0` must grep for `removed_in="0.10.0"` and remove those surfaces and their ignores.
- The first deprecations ship in `0.9.0` and are removed in `0.10.0`:
  - #634: `TradingCustomerRecoveryRunner` without `resource_policies`, and with it the
    SQLite path of `sose composition-recovery`. From `0.10.0` composition recovery requires
    PostgreSQL.
  - #637: `PersistentJobRunner` on persistence without scoped writers.
  - #632: public names reached through a module they were split out of.

## Amendment (2026-11-19): extending a deprecation window

A deprecation window may be **extended** by one or more minors when the removal turns out
to cost more than its notice period justified. Each extension updates `removed_in`
in the warning and records the reason here. It never shortens a window.

- #634 (policy-less `TradingCustomerRecoveryRunner` and the SQLite path of
  `sose composition-recovery`): extended from `0.10.0` to `0.11.0`. Removing the mode
  moves about 75 recovery tests (15 files) from SQLite/memory to PostgreSQL with resource
  policies, which changes their timing semantics. They are ported in dedicated PRs first.

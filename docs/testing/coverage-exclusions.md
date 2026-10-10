# Coverage exclusion ledger

SOSE does not use "untestable" as a synonym for inconvenient. Every exclusion must
belong to one of the categories below and must be narrow enough to audit.

## Categories

| Category | Meaning | Coverage treatment |
| --- | --- | --- |
| Structural | Static typing or import-time constructs with no runtime product behavior | Exclude from denominator |
| Enforced invariant | A branch can only be reached by violating a stronger constructor, closed internal operation set, upstream library lifecycle, or transactional database invariant | Narrow line-level pragma allowed |
| Fault-injection candidate | Reachable only by deliberately corrupting or bypassing supported state, but the guard still expresses useful runtime behavior | Keep in test backlog; do not call impossible |
| Environment/package path | Requires a different installation or dependency envelope | Test in a dedicated packaging envelope |
| Ordinary behavior | Validation, failure, retry, malformed input, stale ownership, outage, domain unhappy path | Must be tested |

## Structural exclusions

Coverage configuration excludes:

- `typing.Protocol` class bodies, including protocols with multiple base classes;
- `TYPE_CHECKING`-only code;
- abstract `NotImplementedError` bodies.

These declarations do not represent executable SOSE behavior.

## Accepted enforced invariants

The following current `# pragma: no cover` sites are accepted because exercising
them would require invalidating an already-tested stronger contract.

| File / line | Invariant |
| --- | --- |
| `backends/simpy.py:275` | SimPy store-put event cannot be processed before callback attachment under the supported scheduling lifecycle |
| `backends/simpy.py:319` | SimPy store-get event callback lifecycle |
| `backends/simpy.py:350` | Store kind is a closed internal set validated before backend reconstruction |
| `backends/simpy.py:454` | Container operation is a closed internal set |
| `backends/simpy.py:457` | SimPy container event callback lifecycle |
| `backends/simpy.py:529` | SimPy resource-request callback lifecycle |
| `backends/simpy.py:626` | Preemptive resource lifecycle executes inside an active SimPy process |
| `persistence/postgres.py:110` | Metadata singleton cannot disappear after transactional bootstrap |
| `persistence/postgres.py:258` | Record changes have a closed internal operation set |
| `persistence/postgres.py:392` | Metadata singleton cannot disappear during revision update |
| `persistence/sqlite_incremental.py:144` | Metadata singleton cannot disappear after transactional bootstrap |
| `persistence/sqlite_incremental.py:288` | Record changes have a closed internal operation set |
| `persistence/sqlite_incremental.py:327` | Writer metadata cannot disappear after a successful compare-and-swap update |
| `core/stores.py:392` | Store-item consume helper is only called after caller-level presence checks |
| `core/engine.py:91` | Engine construction requires a statechart factory |
| `core/engine.py:129` | Engine construction requires a statechart factory |
| `core/engine.py:222` | Engine construction requires a statechart factory |
| `core/durable.py:77` | Attached durable scheduler requires its due callback |
| `core/durable.py:204` | Recovery participants are validated for `rebuild_backend()` before reconstruction |
| `persistence/duckdb.py:132` | Record changes have a closed internal operation set |
| `jobs/config.py:22` | Namespace construction normalizes to a lowercase SQL-safe stem, prefixes digit/empty starts, truncates length, and appends an 8-hex digest; the final regex rejection cannot be reached through any `job_id` input |

These are still defensive guards. The exclusion says the failure branch is not
part of the supported execution state space; it does not claim the Python
statement is physically impossible to trigger with monkeypatching.

## Remaining exclusion debt

No current `# pragma: no cover` site is classified as testable exclusion debt.
Supported packaging failures, malformed SimPy store values, and P2P corrupted-state
guards now have executable evidence and remain in the measured coverage denominator.

## Removed exclusions

The coverage audit has already removed pragmas from:

- reference-conformance module import failures;
- reference-conformance scenario-module import failures;
- malformed custom `EntityFactory` classes.

Those are normal observable error paths and now have executable tests.

The audit has also removed pragmas from:

- missing SimPy optional-dependency import failure;
- malformed native SimPy store values;
- P2P missing seeded receipt guards;
- P2P missing material-demand guards.

## Review rule

A new `# pragma: no cover` is a code-review event, not a convenience. It must:

1. name the invariant in the inline comment;
2. be listed here if it remains after the PR;
3. explain the stronger tested contract that makes the branch unreachable; and
4. be rejected if the path can be exercised through a supported public,
   integration, packaging, or fault-injection envelope.

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
| `backends/simpy.py:318` | SimPy store-get event callback lifecycle |
| `backends/simpy.py:349` | Store kind is a closed internal set validated before backend reconstruction |
| `backends/simpy.py:453` | Container operation is a closed internal set |
| `backends/simpy.py:456` | SimPy container event callback lifecycle |
| `backends/simpy.py:523` | SimPy resource-request callback lifecycle |
| `backends/simpy.py:620` | Preemptive resource lifecycle executes inside an active SimPy process |
| `persistence/postgres.py:100` | Metadata singleton cannot disappear after transactional bootstrap |
| `persistence/postgres.py:210` | Record changes have a closed internal operation set |
| `persistence/postgres.py:307` | Metadata singleton cannot disappear during revision update |
| `persistence/sqlite_incremental.py:114` | Metadata singleton cannot disappear after transactional bootstrap |
| `persistence/sqlite_incremental.py:253` | Record changes have a closed internal operation set |
| `persistence/sqlite_incremental.py:292` | Writer metadata cannot disappear after a successful compare-and-swap update |
| `core/engine.py:91` | Engine construction requires a statechart factory |
| `core/engine.py:129` | Engine construction requires a statechart factory |
| `core/engine.py:222` | Engine construction requires a statechart factory |
| `core/durable.py:76` | Attached durable scheduler requires its due callback |
| `core/durable.py:197` | Recovery participants are validated for `rebuild_backend()` before reconstruction |
| `persistence/duckdb.py:132` | Record changes have a closed internal operation set |

These are still defensive guards. The exclusion says the failure branch is not
part of the supported execution state space; it does not claim the Python
statement is physically impossible to trigger with monkeypatching.

## Remaining exclusion debt

None. Every previously identified reachable exclusion now has executable
evidence. New exclusions remain subject to the review rule below.

## Removed exclusions

The coverage audit has already removed pragmas from:

- reference-conformance module import failures;
- reference-conformance scenario-module import failures;
- malformed custom `EntityFactory` classes;
- missing SimPy optional dependency imports;
- malformed native SimPy store values;
- corrupted P2P receipt/material-demand seed invariants.

Those are normal observable error paths and now have executable tests.

## Review rule

A new `# pragma: no cover` is a code-review event, not a convenience. It must:

1. name the invariant in the inline comment;
2. be listed here if it remains after the PR;
3. explain the stronger tested contract that makes the branch unreachable; and
4. be rejected if the path can be exercised through a supported public,
   integration, packaging, or fault-injection envelope.

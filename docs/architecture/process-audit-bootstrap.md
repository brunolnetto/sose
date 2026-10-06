# Process Audit Reference Bootstrap

## Purpose

W1 reuses the repository's existing reference-conformance evidence without treating the older `ReferenceContract` model as if it already satisfied the newer PC0-PC6 process-canonical contract.

`tests/support/reference_catalog.py` currently contains 17 established reference domains with repository evidence attached to `ReferenceCapability` values. That evidence is valuable provenance, but the capability vocabulary is not equivalent to process maturity.

## Rule

Reference evidence enters W1 through `reference_contract_process_evidence()` as a `ReferenceProcessEvidenceBootstrap`.

A bootstrap is **candidate evidence**, not a `ProcessManifest` and not a maturity promotion.

The following capability translations are accepted because they are direct semantic equivalents:

| Reference capability | Process evidence candidate |
|---|---|
| `STATECHARTS` | `STATECHARTS` |
| `HAPPY_PATH` | `HAPPY_PATH` |
| `SAD_PATHS` | `SAD_PATHS` |
| `RESTART_EQUIVALENCE` | `RESTART_EQUIVALENCE` |
| `RESOURCES` | `FINITE_RESOURCES` |
| `SCHEDULED_WORK` | `TIME_SEMANTICS` |
| `CRASH_RECOVERY` | `FAULT_RECOVERY` |

Every translated claim preserves the exact repository paths already attached to the `ReferenceContract`.

## Deliberately unmapped capabilities

Other reference capabilities remain visible in `unmapped_capabilities` and are not discarded. They simply have no automatic PC-evidence interpretation yet.

Examples include:

- scenarios;
- store selection;
- preemption;
- illegal prerequisites;
- probabilistic transitions;
- immutable occurrences.

A later domain audit may use those sources to support a process claim, but it must do so explicitly.

## No transitive promotion

The bootstrap must not infer stronger claims from nearby evidence.

In particular:

- `HAPPY_PATH` does not prove `E2E_TERMINAL_OUTCOME`;
- `RESOURCES` does not prove `CAPACITY_CONTENTION`;
- `RESTART_EQUIVALENCE` does not automatically prove `DURABLE_STATE`, `REPLAY_IDEMPOTENCE`, or `RECURRING_RECONCILIATION`;
- `SCHEDULED_WORK` does not prove recurring-job continuation;
- reference status does not prove KPIs, ERD, process diagrams, or projection contracts.

## Why bootstrap and manifest are separate

`ProcessManifest` requires semantic details such as:

- trigger identity;
- terminal outcomes;
- resource names;
- sad-path names;
- KPI names;
- process specification path.

The older reference catalog intentionally does not carry all of those details. Automatically manufacturing them would turn provenance reuse into an inference engine and make W1 circular.

Therefore the W1 workflow is:

```text
ReferenceContract
    -> ReferenceProcessEvidenceBootstrap
    -> direct domain review
    -> named process details + additional evidence
    -> ProcessManifest
    -> assessment_complete=True only when the domain audit is complete
```

## W1 audit implication

The 17 existing reference contracts provide a head start, not an automatic maturity score.

The remaining newer process domains require direct evidence discovery. Warehouse Management is already directly audited under the PC contract; Warehouse Fulfillment, Subscription SaaS, Field Service, and Hospitality still require direct process review.

The W1 exit gate remains unchanged: every business-process domain must have a completed, provenance-bound audit and explicit gaps before W2/W4 promotion work is selected.

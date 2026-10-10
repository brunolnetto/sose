# PC6 certified-version terminal-state audit (PRD / TRD / ADR)

## PRD
An immutable BusinessEffectApplied certificate must not validate against the **same entity version** with a different terminal state. An entity at a *later* version may have advanced to another legitimate state; this change does not attempt to infer event history across those later versions.

## TRD / falsification
1. Produce a complete PC6 reference customer flow and certificate.
2. Rewrite the certified entity's state at the same version without changing the certificate (fault injection / corrupt import).
3. The read-only causal auditor must fail closed with an explicit terminal-state contradiction.
4. Preserve the existing version floor and identity checks and historical v1 compatibility.

## ADR
Extend existing single-transaction audit rather than adding a second certificate store. Comparing terminal state only when versions are equal avoids incorrectly rejecting later legitimate entity transitions. Proving that a later-version entity had a certified historical predecessor requires a separate immutable state/event lineage gate; this test does **not** prove it.

## Gate
TDD regression; coverage >=95%; PostgreSQL, SQLite, Python matrix, chaos and frozen historical replay. #406 remains open.

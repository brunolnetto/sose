# PC6 R2R journal restart protocol (PRD / TRD / ADR)

**Stage:** final scoped domain consumer after the Payments recovery PR. The historical scientific v1 bundles are immutable.

## PRD

A recurring Trading Company worker must resume any accepted `accounting.entry_requested.v1` R2R journal after worker death. The journal's committed status, not transport ACK nor an old in-memory stage list, determines how much work remains. Duplicate recovery must not create duplicate posting transitions.

## TRD

- Scope claims to `record_to_report:accounting.entry_requested.v1`.
- The consumer discriminates by **source domain**, not contract name alone: customer settlement from `cards_payments` stages `composition.post_customer_journal`, while purchase-order replenishment from `procure_to_pay` stages `composition.post_replenishment_journal`. Both are persisted under `accounting.entry_requested.v1` and share the R2R runtime, but their command identity and causal origin remain distinct. Unknown source domains fail closed.
- Reconstruct the real `R2REntities` journal identity and its period from the persisted journal entity, then replay the existing R2R statechart rather than infer them from a clock or a fixture stage.
- Rebuild SimPy **at** persisted `SimulationPosition.logical_time`, drain restored resource leases, and only then advance to the command due time. Preserve `drafted`/ `submitted`/ `posted` semantics; once posted, the operation is a no-op except completion of any interrupted resource release.
- Require journal state `posted` before clearing the accepted business Command; deletion is the current reference implementation's staging convention, not a universal business-completion certificate.
- Worker trigger identity, fencing, bounded actions, and checkpoint logic remain unchanged.

## ADR and research gate

Keep R2R domain authority inside its own runtime, not in the composition transport. Do not declare PC6 a universally exactly-once semantic system until explicit completed-effect evidence, PostgreSQL concurrency, network-partition fault injection and replay equivalence are scientifically demonstrated. The staged PR neither changes frozen protocols nor weakens coverage.

# Procure-to-Pay to SOSE mapping

| Domain concept | SOSE primitive | Durable meaning |
|---|---|---|
| Requisition lifecycle | Entity + StateChart | approved/rejected/ordered business state |
| Purchase order lifecycle | Entity + StateChart | supplier-order business state |
| Receipt lifecycle | Entity + StateChart | physical receipt progress |
| Material demand lifecycle | Entity + StateChart | shortage/allocation/consumption state |
| Supplier lead time | Command + DurableScheduler | future receipt/dispatch intent |
| Material lots / discrete stock | Store | durable item and pending put/get semantics |
| Bulk stock quantity | Container | durable quantitative level and terminal operations |
| Receiving dock / inspector | Resource | durable demand/reservation/release semantics |
| Expedite / emergency handling | PreemptiveResource where needed | durable preemption outcome |
| Supplier delay | Scenario Engine | external intervention, not lifecycle mutation |
| Demand spike | Scenario Engine | external environment / policy effect |
| Business audit trail | DomainEvent | immutable facts |
| Requisition → PO → Receipt linkage | causation_id / correlation_id | causal business chain |
| Restart boundary | SimulationPosition | durable logical recovery position |

## Primitive-selection rules

### Store versus Container

Use `Store` when item identity matters:

- serialized material;
- lot / batch;
- discrete receipt line;
- selective SKU retrieval.

Use `Container` when only a fungible quantity matters:

- bulk stock;
- tank / silo quantity;
- aggregate consumable inventory.

A domain implementation may use both when the business needs both item identity and
aggregate quantity, but the example should avoid duplicating the same semantic truth in
two places without an explicit reconciliation rule.

### Resource versus PreemptiveResource

Use a normal `Resource` for ordinary receiving/inspection capacity.

Use `PreemptiveResource` only when the domain explicitly allows higher-priority work
to displace active lower-priority work. Queue priority alone is not preemption.

### Scenario versus StateChart transition

A supplier delay is an external condition. The Scenario Engine may alter attributes,
weights or operational context, but legal lifecycle topology remains owned by the
StateChart.

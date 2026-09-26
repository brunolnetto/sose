# Maintenance / MRO to SOSE mapping

| MRO concept | SOSE primitive | Durable meaning |
|---|---|---|
| Work order lifecycle | Entity + StateChart | maintenance business state |
| Part demand lifecycle | Entity + StateChart | shortage/allocation/consumption state |
| technician | Resource | durable labor demand/reservation/release |
| maintenance bay | PreemptiveResource | durable preemptible capacity |
| spare-part lot | Store | discrete part identity |
| spare-part quantity | Container | aggregate quantitative stock |
| scheduled release | Command + DurableScheduler | future lifecycle intent |
| emergency displacement | ResourcePreemptionResult | durable preemption evidence |
| asset failure | Scenario Engine | external intervention |
| part availability disruption | Scenario Engine | external availability context |
| technician capacity loss | Scenario Engine | external capacity context |
| lifecycle audit | DomainEvent | immutable transition history |
| process trace | correlation_id / causation_id | causal business chain |
| recovery position | SimulationPosition | durable logical restart boundary |

## Primitive-selection rules

### Store + Container

The reference uses both because part identity and quantity are separate business facts.
Feasibility must be established for both before withdrawal begins.

### Resource + PreemptiveResource

Technician capacity is ordinary constrained capacity. The maintenance bay is
preemptible because emergency work may displace active maintenance.

### Scenario + StateChart

Scenarios alter operational context. They do not directly mutate WorkOrder or
PartDemand state; normal reconcilers dispatch the corresponding StateChart events.

# Manufacturing -> SOSE mapping

| Manufacturing concept | SOSE primitive |
|---|---|
| Production order lifecycle | Entity + StateChart |
| Operation lifecycle | Entity + StateChart |
| machine | PreemptiveResource |
| operator | Resource |
| raw material lot | Store |
| bulk raw-material quantity | Container |
| WIP | Store |
| finished-goods quantity | Container |
| setup/process timing | DurableScheduler / Command |
| breakdown | StateChart transition + preemptive demand |
| maintenance displacement | ResourcePreemptionResult |
| downtime/yield intervention | Scenario |
| process trace | correlation_id / causation_id |
| recovery position | SimulationPosition |

## Modeling discipline

A business state may claim an operational effect only after durable evidence of
that effect exists. Resource acquisition and quantitative operations therefore
gate lifecycle transitions rather than run beside them.

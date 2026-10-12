# OD-5a recurring SimPy dispatch bridge — first #485 source-step slice

Tracker: #485 under #477. Relies on merged #479, #482, #484 and #486. **Not promotion of the production-native SimPy mechanism.** The five-arm prospective scientific execution freeze remains `NOT_ADMITTED`.

## PRD

The first recurring adapter must prove that an actual externally invoked `SimulationJobState` can own a **scoped source-step claim** and advance one committed physical organizational action through the native `SimPyBackend` event scheduler, without losing source truth or duplicating physical allocations after real worker death.

## TRD — bounded implementation

- `initialize_od5a_recurring_job` freezes compiled arm/model and pilot workload provenance against the organization's authoritative source Entity and binds one initialized `SimulationJobState`. It refuses retroactive adoption of already advanced source history.
- `claim_od5a_recurring_tick` registers one deterministic step ID in a transaction with a versioned `od5a_runner_lease` entity scoped to that organization, unique owner, strictly increasing epoch and expiration. A different live owner cannot steal the claim. Recovery of an expired incomplete trigger requires `recover=True`, then increments the epoch.
- `execute_od5a_claimed_tick` reconstructs a disposable native `SimPyBackend`, schedules the next authoritative action at the organization-local simulated timestamp, and runs exactly that callback. **In the callback's authoritative transaction**, validate the exact runner epoch/owner/expiry and unchanged scheduled action, advance the existing physical reducer via `_advance_od5a_in_uow`, commit the DomainEvent and resource ledger, update `SimulationJobState` clock/tick/completed-trigger ID, and release the runner lease **atomically**.
- `run_od5a_recurring_tick` is a bounded recurring invocation wrapper. Retrying the latest committed trigger returns `replayed` without another physical stage. Death after claim requires expiration/recover; death before UoW COMMIT rolls back source+checkpoint together; death after COMMIT leaves both intact.
- No global `SimulationPosition` or simulated global clock; each organization's source Entity owns logical time. Physical reservation arbitration remains the existing authoritative OD-5a reducer. Do not serialize `simpy.Environment`, Process, Event or pending callbacks.

## TDD falsification

Five compiled reference arms compare complete physical source state/events/resource leases against the standalone durable reducer. Real OS processes die after claim and after source COMMIT; stale fencing tokens cannot dispatch. Injected exception after job-state staging but before commit verifies source DomainEvent + job checkpoint rollback. Duplicate trigger replay and interleaved independent organization clocks are tested.

## ADR — stop rule and next incremental work

This is a **SimPy-dispatched, reducer-authoritative source step**, not the full production SimPy physical backend. The physical allocation algorithm currently remains `od5a_durable._dispatch`; replacing its semantics with native persistent SimPy resource arbitration requires three-way conformance against the independent pilot and red/green physics probes. The boundary reconciler from #484 still needs to be integrated into recurring execution under the *same fenced worker protocol*, with exact crash windows. The runner's SQLite lease/checkpoint baseline must be certified separately on PostgreSQL under real contending processes and database failures before any production promotion. Scientific DOE, independent hash seal, and a claim of full #485 completion are prohibited.

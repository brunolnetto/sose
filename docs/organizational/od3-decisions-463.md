# #463 — deterministic bounded OD-3 policy decisions

Two **real and different decision predicates**: adaptive follow-up contacts
oldest overdue eligible work; cherry picking prioritizes the shortest eligible
job, but an old waiting item receives forced priority after the configured
starvation limit. Cooldown, cost, null action, forbidden future observations,
owner segregation, replay conflicts and same-cost observational sham are
explicit. No process-global RNG; decisions are a pure function of the
supplied time, queue, observation identity and policy configuration.

This is **not #463 closure**: decisions currently persist an observational
DomainEvent without applying a physical queue mutation or debiting an actual
actor-time ledger. The next increment must introduce owner-authorized durable
queue/service action, policy activation and cost ledger, restart/writer fencing,
and paired CRN/sham/negative-outcome experiment on the #462 actor contract.
Do not confuse this with the historical A1 synthetic runner supporting only
BATCHING.

## Wave 1 durable effect/cost reconciliation (incremental)

Before each *new* operational policy application, the OD-3 queue now compares
its projected charged microseconds, last action/observation timestamps,
queue version, action-derived item order and applied observation count against
its chronological `od3.queue_effect_applied` and `od3.policy_decided` pairs.
A projection that shifts the rank or invents paid-policy time without a durable
effect is rejected transactionally rather than accepted as the next policy
input. Memory/SQLite tests cover corruption and valid continuing decisions.

This is a **bounded queue-ledger invariant**, not a real actor service
reservation or conserved physical worker time. The queue's public UoW event
source is not protected independently (#545); durable actor-time debits in
#462 must be approved before a cross-service charge is considered
scientifically authoritative. Do not close #463 from this slice alone.


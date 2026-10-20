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

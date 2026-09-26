# Manufacturing scenarios

## Machine downtime

A one-shot finite scenario marks an external downtime window. The scenario does
not itself mutate ProductionOrder state; domain orchestration observes the
environment and decides whether a breakdown transition is required.

## Yield degradation

A finite scenario lowers effective yield during a production window. Output
quantity is derived from durable input quantity plus scenario context.

## Demand surge

A finite scenario increases production pressure and can create additional work
or resource contention.

## Rules

- trigger frequency and activation duration are modeled separately;
- finite scenarios use one-shot triggers unless repetition is explicitly intended;
- restart must not reactivate an already consumed one-shot intervention;
- scenario effects alter context/policy, not StateChart topology.

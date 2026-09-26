# Procure-to-Pay scenarios

The reference domain uses scenarios as external operating conditions, not as a second
lifecycle engine.

## Supplier delay

Effect:

- extend or alter the effective supplier-delivery context;
- make the delayed path observable;
- preserve the already-durable order and future work.

The scenario must not mutate a PurchaseOrder directly.

## Receiving congestion

Effect:

- reduce effective receiving capacity or bias work toward waiting behavior;
- leave resource ownership semantics durable.

The backend may model queue mechanics, but the domain result must remain restart-safe.

## Demand spike

Effect:

- increase material demand pressure;
- make shortage / backorder paths easier to activate;
- never create hidden inventory.

## Supplier outage

Effect:

- prevent or strongly discourage normal supplier progression during the activation
  window;
- allow normal behavior again after expiry if the domain lifecycle permits it.

## Scenario acceptance criteria

For each implemented scenario:

1. activation decision is durable;
2. effect is deterministic for the same random seed and semantic state;
3. restart during activation preserves equivalent behavior;
4. scenario code does not mutate entities outside normal commands / StateCharts;
5. expiry removes the intervention without erasing business history.

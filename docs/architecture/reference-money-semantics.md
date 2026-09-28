# Reference Money Semantics Audit

## Scope

This audit reviews monetary semantics across the promoted Reference implementations.
It follows the same rule as the runtime consolidation work:

> promote a core abstraction only after repeated durable semantics are demonstrated.

The audit distinguishes **money** from generic numeric quantities. In particular,
Procure-to-Pay currently uses `amount` for Store/Container quantities, but its
executable Reference slice explicitly stops before invoice approval, three-way
matching, and payment. P2P therefore does **not** currently provide executable
monetary evidence.

## Executable money-bearing References

| Reference | Monetary lineage | Representation before this audit | Currency before this audit |
|---|---|---|---|
| Cards & payments | Payment | float attribute | explicit on Payment |
| Order-to-Cash | SalesOrder -> Receivable | float attribute | propagated |
| Record-to-Report | JournalEntry -> ReconciliationItem -> Adjustment | float attribute | missing |
| Insurance | Claim -> Reserve -> Payment | float attribute | missing |
| Credit & Loans | Application -> Loan -> Installment -> Payment / Restructure | float plus local cent allocation | only Application/Loan |

## Findings

### 1. Currency provenance was inconsistent

R2R persisted accounting amounts without currency. Insurance persisted claim,
reserve, and payout amounts without currency. Credit & Loans carried currency on
the application and loan but dropped it on servicing obligations and payment
occurrences.

This is a durable semantic gap, not presentation metadata: after restart, a
numeric amount without its currency cannot be interpreted independently.

This audit fixes those lineages and adds executable regression coverage in
`tests/test_reference_money_semantics.py`.

### 2. Numeric representation is still inconsistent

The References still use Python `float` for persisted monetary values.

Credit & Loans is the strongest counterexample to naive float arithmetic:
installment allocation converts principal to integer cents, allocates the
remainder deterministically, then projects values back to float. Payment
application rounds balance projections to two decimals.

Insurance partial payout currently derives a half-payment from the persisted
amount. R2R mostly copies amounts without arithmetic. Cards & Payments and O2C
mostly carry amounts through lifecycle transitions rather than performing
ledger arithmetic.

These are materially different arithmetic contracts. The audit therefore does
not replace them with a generic helper merely because all fields are called
`amount`.

### 3. Currency and amount form one semantic value

Where an entity owns a monetary amount, currency must travel with that amount
through derived durable entities. Currency is not inferred from the domain or
from a default after the fact.

The new regression suite makes that lineage explicit for all current
money-bearing References.

### 4. P2P must not be used as Money evidence yet

The P2P blueprint includes future financial semantics, but its current Reference
implementation is supply-side operational flow. Container request `amount`
means quantity.

Treating those fields as money would conflate two unrelated numeric domains and
would be exactly the kind of abstraction-by-name that SOSE's consolidation
rules are intended to avoid.

## Decision: no core Money primitive yet

The evidence is sufficient to establish two cross-domain invariants:

1. monetary values require explicit currency provenance;
2. derived monetary entities must preserve that currency.

It is **not** yet sufficient to freeze a core arithmetic representation.

A future `Money` primitive would need an explicit contract for:

- representation: integer minor units vs Decimal;
- currency code validation;
- currency-specific scale (not every currency has two decimal places);
- rounding mode and allocation of remainders;
- comparison and zero semantics;
- serialization/backward compatibility;
- cross-currency operations (normally forbidden without an explicit FX value);
- immutable occurrence semantics for postings/payments;
- restart-safe idempotent arithmetic.

Until those questions are demonstrated by multiple References, domain-local
arithmetic remains preferable to a premature core abstraction.

## Promotion gate for future monetary References

A new Reference that claims monetary semantics should provide executable evidence
for:

- explicit currency on the durable monetary owner;
- currency preservation across derived obligations/occurrences;
- deterministic allocation when splitting values;
- explicit rounding policy when arithmetic can create fractions below the
  supported currency scale;
- idempotent application across retry/restart boundaries;
- no inference of currency from process context alone.

## Minor-unit conformance result

The focused rounding pass adds executable evidence where arithmetic actually
occurs rather than imposing arithmetic rules on References that only carry
amounts through lifecycle transitions.

Credit & Loans now:

- rejects USD principal/payment inputs below the cent scale;
- allocates principal in integer cents;
- assigns allocation remainder deterministically to the final installment;
- preserves exact principal minor units for odd-cent totals.

Insurance now:

- rejects claim/reserve values below the Reference's two-decimal minor-unit
  scale;
- computes partial payout in integer minor units;
- leaves an odd-cent remainder for final completion rather than persisting a
  fractional cent;
- proves the partial-then-complete result survives runtime rebuild.

Cards & Payments, O2C, and R2R do not currently perform arithmetic that can
create sub-minor-unit values, so this audit deliberately does not add synthetic
rounding rules to them.

### Float-facing boundary rule

The current Insurance and Credit & Loans Reference APIs still accept Python
`float` inputs. Their minor-unit adapters therefore need to distinguish normal
IEEE-754 representation noise from an actual fractional minor unit without
inventing a magnitude-specific cutoff.

The conformance rule is now:

1. scale the float into the Reference minor unit;
2. compare it with the nearest integer minor unit;
3. tolerate ordinary binary error using a small ULP-aware window;
4. cap that window strictly below half a minor unit.

This admits large values that are still represented exactly at a cent boundary
even when their ULP exceeds the old conservative threshold, while a represented
half-cent remains invalid. It does **not** claim that arbitrary decimal intent
can always be recovered from an IEEE-754 float at extreme magnitudes. If future
References need stronger guarantees, that is evidence for an exact external
representation such as integer minor units or Decimal at the API boundary—not
for a generic core Money type by itself.

## Stopping decision

The Money investigation is complete for the current Reference set.

The evidence supports Reference-level contracts for currency provenance,
minor-unit boundaries where arithmetic occurs, deterministic allocation, and
restart-safe application. It does **not** show a repeated cross-domain
arithmetic protocol strong enough to justify a core `Money` primitive.

A future domain may reopen this decision only if it introduces repeated
requirements such as currency-specific scale, FX conversion, rated usage,
tax allocation, or immutable ledger posting that cannot be expressed cleanly
with the existing domain-local contracts.

Until then, adding a core Money value object is explicitly out of scope.

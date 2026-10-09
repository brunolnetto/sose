# Trading Company PC6: existing implementation audit and promotion sequence

**Evidence stage:** regression gate added; NOT a new PC6 maturity claim.

## Reuse before abstraction

Inspection of `sose.composition` in main identified existing durable
`BoundaryMessage`, `BoundaryDelivery`, `BoundaryConsumption`,
`BoundaryService`, consumer registry, claim leases and fencing, plus
`trading_company_customer` and `trading_company_replenishment` composed
flows. Replacing these with a parallel envelope would violate ADR-0006.

This PR adds a *single two-path executable contract test*, rather than
duplicating the message model or domain-specific business implementation.
The test asserts that all exercised deliveries reach CONSUMED with unique
identities and durable effects; messages share the expected path correlation;
required customer/replenishment contract names are exercised; and Warehouse
Fulfillment creates no authoritative local InventoryLot in composed mode.

## Existing requirements

- PRD-0003 / TRD-0003 / ADR-0006 are accepted and authoritative.
- Existing `test_trading_company_recovery_conformance.py` already tests
  SQLite-based restart, lease expiration, fencing and two business paths.
- Existing standalone PC5 semantics are preserved.

## Remaining promotion gate

Do not mark newly composed PC6 as complete solely on the contract smoke test:
rerun ownership, restart, duplicate-delivery, PostgreSQL portability and
fencing suites; verify ProcessManifest provenance and review any unexercised
TRD-0003 contracts. Actual PC6 maturity promotion must be evidence-bound.

## Cross-domain scientific boundary

Trading Company PC6 tests durable composition, not O2C/MRO experimental
agency. It does not justify sharing their DomainReference adapters or
altering either scientific preregistration, and must not change
Manufacturing v1 official hashes.

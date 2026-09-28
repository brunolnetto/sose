# Subscription / SaaS Reference

## Status

**Reference implementation.**

This Reference is grounded in product/subscription lifecycle patterns documented
by TM Forum product inventory and commercial SaaS platforms: the commercial
subscription lifecycle is distinct from the product/entitlement assigned to the
customer, and changes/cancellations may become effective at future boundaries.

The executable slice deliberately excludes billing, price, tax, and proration.
Money remains closed unless a future domain repeats a genuinely shared
arithmetic contract.

## Executable slice

One active Basic subscription owns one active Basic entitlement.

A future plan change:

1. creates a durable ChangeRequest;
2. owns one future effective boundary through ScheduledWork;
3. becomes applied at that boundary;
4. supersedes the old Entitlement;
5. installs one new Entitlement;
6. records one immutable plan-change occurrence.

Cancellation-at-period-end is different from immediate deletion. It moves the
commercial Subscription to cancellation_pending, cancels still-pending
amendments, and schedules both Subscription end and Entitlement revocation at
term end. The customer may withdraw cancellation before the boundary.

## Architectural result

SOSE already had scheduled work, but this Reference demonstrates that
future-effective intent itself is domain truth. A ChangeRequest is not merely a
timer, and Entitlement inventory is not the same entity as the commercial
Subscription.

No new core primitive is needed: DurableScheduler already owns reconstruction of
the time boundary, while domain entities own what the boundary means.

## Stopping rule

The Reference stops after proving one pending future amendment, idempotent
post-boundary reconciliation, entitlement replacement, cancellation-at-period-
end, cancellation withdrawal, amendment cancellation, immutable change
occurrence, and restart equivalence.

Trials, billing schedules, metering, invoicing, proration, discounts, payment
failure, seat quantities, and marketplace settlement remain breadth or Money
work and are intentionally excluded.

## References

- TM Forum TMF637 Product Inventory Management / Product Inventory component.
- Microsoft Marketplace SaaS subscription lifecycle.
- SAP Subscription Billing lifecycle descriptions.

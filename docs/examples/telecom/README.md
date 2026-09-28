# Telecommunications

Status: **Reference implementation**

This Reference models a postpaid mobile subscription from product order through
service-order decomposition and asynchronous network activation, followed by
usage capture and service-assurance handling.

The domain deliberately separates:

- commercial ProductOrder;
- technical ServiceOrder;
- long-lived SubscriptionService inventory state;
- immutable UsageRecord occurrences;
- NetworkAlarm fault evidence;
- customer/operations-facing TroubleTicket lifecycle.

That separation follows the current TM Forum ODA/Open API vocabulary rather
than collapsing telecom fulfillment, inventory, usage, and assurance into one
generic subscription state machine.

The executable slice covers 5G/eSIM-style activation semantics without modeling
vendor-specific network commands. It also deliberately stops before charging and
billing: usage is non-rated evidence, so Telecom does not reopen the core Money
decision.

See [specification.md](specification.md) for the authoritative contract.

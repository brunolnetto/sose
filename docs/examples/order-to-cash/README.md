# Order-to-Cash

Status: **Partial**

This domain is being promoted from Blueprint into a reference-grade cross-entity commercial and financial example.

It separates commercial commitment (`SalesOrder`), financial obligation (`Receivable`), and overdue recovery (`CollectionCase`) so credit holds, partial fulfillment, invoicing, disputes, overdue handling, and collection remain independently durable facts.

See [specification.md](specification.md) for the authoritative contract.

Reference-grade promotion still requires durable credit/fulfillment resources, invoice-to-receivable creation, due/overdue ScheduledWork, collection assignment, scenario recovery, end-to-end paths, and restart equivalence.

# Order-to-Cash

Status: **Reference implementation**

This domain is being promoted from Blueprint into a reference-grade cross-entity commercial and financial example.

It separates commercial commitment (`SalesOrder`), financial obligation (`Receivable`), and overdue recovery (`CollectionCase`) so credit holds, partial fulfillment, invoicing, disputes, overdue handling, and collection remain independently durable facts.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation includes credit and fulfillment gating, explicit partial fulfillment, invoice-to-receivable causality, durable due/overdue scheduling, CollectionCase recovery, collection-agent ownership and promise follow-up, finite scenario recovery, illegal prerequisite rejection, and restart equivalence for both ScheduledWork and ResourceDemand boundaries.

# Order-to-Cash

Status: **Partial**

This domain is being promoted from Blueprint into a reference-grade cross-entity commercial and financial example.

It separates commercial commitment (`SalesOrder`), financial obligation (`Receivable`), and overdue recovery (`CollectionCase`) so credit holds, partial fulfillment, invoicing, disputes, overdue handling, and collection remain independently durable facts.

See [specification.md](specification.md) for the authoritative contract.

Implemented operational evidence now covers credit/fulfillment gating, invoice-to-receivable causality, durable due/overdue scheduling, collection assignment/follow-up, partial fulfillment, and finite scenario recovery. Reference promotion still requires broader restart-equivalence coverage across the remaining cross-entity boundaries.

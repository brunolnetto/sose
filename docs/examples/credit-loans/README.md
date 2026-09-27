# Credit & Loans

Status: **Partial**

This reference domain models application underwriting, durable credit decision,
loan origination, recurring installment obligations, partial repayment,
delinquency, collections, default, and restructuring.

The key design choice is that financial history is occurrence-based:

- `Payment` is immutable evidence;
- `Installment` is the obligation being serviced;
- `DelinquencyCase` and `CollectionCase` preserve recovery history;
- `Restructure` supersedes obligations without rewriting them.

See [specification.md](specification.md) for the executable contract.

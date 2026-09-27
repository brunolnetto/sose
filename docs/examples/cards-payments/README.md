# Cards & Payments

Status: **Partial**

This domain is being promoted from Blueprint into a reference-grade transactional example.
It focuses on authorization/capture/settlement, pre-capture reversal, post-settlement refund,
and an independent correlated dispute/chargeback lifecycle.

See [specification.md](specification.md) for the authoritative contract.

Reference-grade promotion still requires durable settlement/retry scheduling, processor
availability gating, dispute deadlines, end-to-end happy/sad execution, and restart equivalence.

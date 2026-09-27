# Cards & Payments

Status: **Reference implementation**

This domain is being promoted from Blueprint into a reference-grade transactional example.
It focuses on authorization/capture/settlement, pre-capture reversal, post-settlement refund,
and an independent correlated dispute/chargeback lifecycle.

See [specification.md](specification.md) for the authoritative contract.

The reference implementation includes processor-gated authorization/settlement/refund,
durable settlement and retry schedules, pre-capture reversal, scheduled dispute evidence,
analyst-gated chargeback resolution, finite outage recovery, and restart equivalence.

# Logistics & Transport

Status: **Reference implementation**

This example is being promoted from the architecture blueprint into a reference-grade domain.
The first slice defines durable business entities, exact StateCharts, probabilistic
last-mile outcomes, scenarios, the persistent model contract, invariants, and executable
topology evidence.

See [specification.md](specification.md) for the authoritative domain contract.

The reference implementation includes durable pickup scheduling, hub Store queues,
capacity-gated movement, distinct failed/retry attempt identities, finite courier
capacity disruption/recovery, and restart-equivalence evidence.

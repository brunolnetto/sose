# Domain Warehouse boundary

SOSE has two persistence responsibilities that must not be conflated.

```text
SOSE
├── Engine OLTP
│   └── execution/recovery truth
└── Domain Warehouse
    └── simulated business-world truth
```

## Engine OLTP

Engine persistence owns execution mechanics: job checkpoints, simulation position,
commands, scheduled work, resources, stores, containers, scenario runtime state,
ownership epochs and fencing.

Its primary question is:

> How can this simulation continue correctly from where it stopped?

Authoritative EnginePersistence adapters therefore need the conformance guarantees
defined by the Engine OLTP qualification suite.

## Domain Warehouse

A DomainWarehouse owns current business entities produced by the simulated domain,
for example MRO WorkOrders and PartDemands. Its primary question is:

> What is the current state of the world being simulated?

The warehouse does **not** inherit EnginePersistence requirements such as job leases,
fencing epochs, ScheduledWork reconstruction, or runtime resource recovery.

`DomainMutation` gives every business-state write an immutable identity. Applying the
same mutation again is a no-op; reusing its identity for different content is an
error. This is the primitive needed to recover safely when Engine OLTP and the domain
warehouse are physically different systems.

## Atomicity boundary

SOSE must not pretend that two independent databases share one ACID transaction.
Today the Engine still stores entities in its legacy Persistence transaction so
scheduled transitions remain atomic.

Physical separation will use a durable engine-side mutation/outbox protocol:

```text
Engine transaction
  ├── commit execution checkpoint
  └── prepare DomainMutation
              │
              ▼
       DomainWarehouse.apply()
              │
              ▼
Engine transaction marks mutation delivered
```

A crash at any boundary is recoverable because `DomainMutation.mutation_id` is
idempotent and immutable.

The MRO reference migration is the first executable consumer of this protocol.

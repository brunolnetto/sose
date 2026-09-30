# End-to-end recurring product contract

The product-level SOSE contract is now exercised as one workflow rather than only
as isolated unit APIs.

The supported user journey is:

```text
sose init
   |
   v
validated DomainConfig
   |
   v
authoritative Persistence
   |
   v
durable SimulationJob
   |
   +-- one scheduler occurrence -> bounded logical tick batch
   |
   +-- process exits
   |
   +-- config edit + explicit apply -> config revision
   |
   +-- next scheduler occurrence -> resume same durable world
   |
   +-- analytical sinks receive committed events through the outbox
```

## MRO acceptance path

The executable product test proves:

1. scaffold MRO from the builtin catalog;
2. select incremental SQLite as authoritative storage;
3. change bootstrap quantity before initialization;
4. disable a runtime automation policy;
5. attach a JSONL analytical sink;
6. execute the first scheduler occurrence;
7. close/reopen persistence through a fresh job construction;
8. observe the WorkOrder waiting for material;
9. edit and explicitly apply the runtime policy;
10. execute the next scheduler occurrence;
11. observe the same WorkOrder advance using config revision 2;
12. verify the analytical sink received committed output.

No end-to-end helper or long-lived process is used.

## Scheduler identity

A second acceptance test invokes the same scheduled occurrence twice through
independent CLI/job constructions and proves the second invocation is idempotent.

This means an external scheduler may safely retry an occurrence after losing the
first process response:

```text
scheduler occurrence
    |
    +-- process A -> committed
    |
    +-- retry process B -> same durable result, no extra tick
```

## Product boundary

This is the target abstraction exposed to users:

```text
Parameterized Domain
        +
Authoritative Store
        +
Durable Recurring Job
        +
Optional Analytical Sinks
```

The domain owns business meaning. The job owns recurring orchestration and
checkpointing. Persistence owns authoritative durable truth. Sinks observe
committed truth and must not become authoritative by accident.

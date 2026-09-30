# MRO Engine OLTP / Domain Warehouse reference

MRO is the first reference domain to persist its simulated business world outside the Engine OLTP boundary.

The current migration stage deliberately keeps Entity records in Engine OLTP as **execution shadows** because the statechart kernel still requires entity reads inside its authoritative transaction. They are not the public DomainWarehouse contract.

`sync_mro_domain()` projects WorkOrder and PartDemand versions through the durable `DomainMutationOutbox`. The reference test proves:

1. Engine OLTP and DomainWarehouse are physically independent objects.
2. a process can die after preparing business mutations but before warehouse write;
3. reopen discovers the pending mutations from Engine OLTP;
4. replay delivers them idempotently;
5. continued execution reaches the same DomainWarehouse state as an uninterrupted run.

The next kernel migration removes the execution shadow by making statechart entity access warehouse-aware while preserving scheduled-work atomicity through the same outbox protocol.

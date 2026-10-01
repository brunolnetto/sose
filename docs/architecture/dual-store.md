# Engine OLTP and Domain Warehouse

SOSE separates execution durability from the simulated business world.

```toml
[domain]
name = "mro"

[persistence]
adapter = "sqlite_incremental"
[persistence.options]
path = "state/engine.db"

[domain_warehouse]
adapter = "sqlite"
[domain_warehouse.options]
path = "state/domain.db"

[job]
id = "mro-recurring"
```

`persistence` is the Engine OLTP. It stores checkpoints, commands, events, scheduled work, runtime resource state and the durable DomainMutation outbox. It answers: **how does execution resume correctly?**

`domain_warehouse` stores current simulated business entities. It answers: **what is the current state of the simulated business world?**

PostgreSQL is also supported for the domain warehouse:

```toml
[domain_warehouse]
adapter = "postgres"
[domain_warehouse.options]
dsn_env = "SOSE_DOMAIN_DATABASE_URL"
namespace = "mro"
```

The two stores may use the same database technology, but they are independent contracts. DomainWarehouse does not inherit Engine OLTP leases, fencing or runtime-resource requirements.

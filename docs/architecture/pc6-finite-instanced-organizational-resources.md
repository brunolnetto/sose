# ADR — finite and instanced organizational resources (PC6)

Status: **proposed opt-in contract**, not an authoritative allocation migration.
Related: #406, #432–#434. Legacy Manufacturing/O2C/MRO v1 fixtures and digests remain frozen.

## PRD: business semantics

A resource **type** describes a capability (technician, forklift, dock, processor); it does not imply supply. A **pool** identifies a finite supply of that capability in a defined organizational ownership scope. An **instance** identifies one indivisible physical or logical unit within an enumerated pool. **Available capacity** is a time-dependent subset of installed capacity after downtime, calendars, reservations, and other constraints.

Two customers may share a processor, but not a technician merely because both call the resource `technician`. Two independently owned warehouses may each contain a `forklift-01`; the physical instances are distinct. Conversely, a shared dock must serialize incompatible activities even when those activities belong to separate organizations.

A resource address includes `(scope, organization_id, resource_type, pool_id, instance_id)`. Scope is exactly `organization` or `shared`; shared pools must not silently acquire a local owner. Address encoding is canonical JSON, so user-supplied punctuation cannot make two distinct resource identities equal.

## TRD: implementable invariants

- Pools declare positive finite integral capacity. Fungible pools supply that many interchangeable integer units; instanced pools enumerate distinct indivisible instances and their count equals declared nominal capacity.
- At any instant, the sum of active quantities in a fungible pool cannot exceed its effective capacity. In the initial pure contract, validate against declared capacity; calendar/availability reductions are **not** assumed away, and will need an authoritative time-aware calculation.
- For instanced pools, every active claim names an instance, each instance appears at most once, and each claim consumes exactly one unit. Prevent duplicate claim identities and attempts to allocate another organization's scoped pool.
- The current `ResourcePoolContract.validate_active()` is a **pure falsifier** for a supplied set of concurrent claims; it does not create an OLTP reservation and cannot, by itself, prevent races.
- `PostgresPersistence.business_resource_guard()` accepts an opt-in typed `ResourceAddress`. Same address blocks across independent PostgreSQL connections for the duration of a multi-commit critical section. Distinct organization-scoped pools and different physical instances are allowed to overlap. Legacy string-based lock names retain their exact behavior.
- Advisory guards are neither authoritative resource claims, lease/fencing certificates, nor business effects. The PostgreSQL `hashtext` advisory-lock key has a finite collision domain; treating guard keys as cryptographically collision-free is forbidden.

### Executable test matrix

| Configuration | Expected overlap of two workers |
|---|---|
| Same shared pool, one exclusive processing slot | **No** |
| Equal names but different organization owners | **Yes** |
| Same named forklift instance | **No** |
| Two distinct forklift instances within same shared pool | **Yes** |

The dedicated PostgreSQL CI gate exercises this matrix with separate connections. Unit tests falsify overcommit, duplicate instance use, foreign organization claims, malformed pools and lock-key aliases.

## ADR: architectural placement and limitations

**Decision:** model explicit resource ownership and identity as immutable additive contracts before migrating existing engine definitions. Keep the current SimPy runtime backend ephemeral and its durable EnginePersistence authoritative. Do **not** add a global organizational mutex; it would erase legitimate parallelism and distort synthetic throughput measurements.

**Not decided/implemented in this PR:** authoritative time-interval reservations, transactional compare-and-set on occupancy, calendars/skills, flexible demand quantities, resource breakdown/recovery, ownership transfer, priority/preemption semantics across tenants, per-job logical clock reconciliation or durable cross-worker allocation receipts. A capacity-2 pool serialized by a single typed pool guard is not a two-slot reservation engine; use named instance guards or an actual transactional resource allocator. A passing typed-guard test does not close #406.

Next falsification requires a shared resource with capacity >1 and time-interval reservations under concurrent workers, plus separate local pools that progress without serializing each other's resource operations. Compare the full durable reservation ledger, causal events and per-organization temporal positions after crash/restart with an uninterrupted reference. Fail closed if availability, idempotency or fencing cannot be proved.

## Migration gate

Opt-in API only; no source fixture default identities, historical digests or legacy strings rewritten. Require Python 3.12–3.14, 95% project coverage floor, PostgreSQL-specific assertions, chaos tests, benchmarks and historical organizational v1 replay before integration.

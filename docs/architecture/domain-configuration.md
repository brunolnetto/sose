# Parameterized executable domains

SOSE domains expose configuration through `DomainDefinition`.

The goal is that orchestration code never needs to know bespoke keyword
arguments for MRO, Telecom, Hospitals, or any other Reference.

## Contract

Each executable domain declares:

- a stable name;
- a human-readable description;
- a validated `DomainConfig` model;
- a runtime builder;
- a seed/bootstrap function.

Example:

```python
definition = catalog.get("mro")
config = definition.parse_config(
    {
        "quantity": 4,
        "technician_capacity": 3,
        "tick_step": timedelta(minutes=30),
    }
)

ids = definition.seed(persistence, config)
context, engine = definition.build_runtime(
    persistence,
    config,
    config.start_at,
    0,
)
```

## Ownership rule

Common orchestration parameters belong in the base config when they mean the
same thing everywhere:

- `start_at`;
- `tick_step`;
- `random_seed`.

Business meaning stays domain-specific:

- MRO quantity and maintenance capacity;
- hospital bed/ICU configuration;
- telecom provisioning/activation parameters;
- warehouse allocation/stock configuration;
- subscription term/change parameters.

Do not flatten these into an untyped `dict[str, object]`.

## Configuration lifecycle

Domain configs are frozen Pydantic models. They are:

- validated before runtime construction;
- JSON serializable;
- safe to store alongside a durable job definition;
- replaceable between job runs through an explicit new configuration revision.

A running tick must use one resolved configuration snapshot.

Parameter mutability is explicit:

- `runtime` — may change between completed triggers and takes effect on the next
  orchestration boundary;
- `bootstrap` — contributes to already materialized durable truth and cannot be
  changed after job initialization.

By default, `tick_step` and `random_seed` are runtime-mutable while
`start_at` and structural domain parameters are bootstrap-only. Every builtin
domain now also exposes at least one **domain-specific runtime control**.

Runtime controls are limited to policy/outcome/automation inputs that can safely
affect future reconciliation without rewriting already-materialized durable
truth. Examples include automatic supply seeding, approval/assessment outcomes,
collection behavior, demand-response activation, settlement/close timing, and
explicit `auto_progress_*` controls for otherwise fully automatic References.

Identity, initial capacity, initial inventory, principal/amount inputs, and
other bootstrap materialization inputs remain bootstrap-only unless a domain
can prove a safe migration semantic.

Business changes that belong to the simulated world should still be modeled as
durable domain state/events rather than configuration mutation.

## Operational parameter depth

Every promoted Reference now has a validated config model. Configuration depth
is intentionally domain-specific rather than forced into one generic schema.

The domains that were previously runtime-only now expose operational knobs:

| Domain | Operational configuration examples |
| --- | --- |
| Transit | vehicle/block identity, first-trip delay, duration, layover |
| Telecom | customer/offering, access technology, SIM type, provisioning capacity |
| Aviation | rotation identity, crew/inspection/maintenance capacity, queue capacities |
| Logistics | service level, route, resource capacity, hub queues, pickup delay |
| Hospitality | room inventory count and room type |
| Field Service | territory, required/wrong skill, technician and parts capacity |
| Cards & Payments | amount, currency, processor capacity |
| Energy / Utilities | customer topology, secondary service point, quantity/unit |
| Subscription / SaaS | customer, initial plan, term duration |
| Warehouse / Fulfillment | demand quantity, primary/substitute stock, substitution policy |

Other References already expose their principal business input such as MRO
quantity/capacity, hospital acuity, insurance claim amount/type/currency,
credit principal/installments, construction quantity/prerequisite, and finance
amount/currency.

Executable tests must prove that an advertised parameter changes durable seed
state, capacity, or scheduling behavior. A field that is merely accepted by a
Pydantic model but ignored by the domain is a configuration defect.

## Rollout rule

The rollout should not introduce parameters merely because a constant exists;
only meaningful user-facing configuration belongs in the public domain config.

A Reference is considered configuration-ready when:

1. its config contains at least one domain-specific business/operational knob;
2. its definition maps the knob into runtime/seed behavior;
3. an executable test observes the changed durable truth;
4. defaults reproduce the Reference's canonical scenario.


## Runtime-control conformance

The builtin catalog is horizontally tested so every domain must expose at least
one domain-specific runtime-mutable field in addition to `tick_step` and
`random_seed`.

Parameter discovery reports each field as `runtime` or `bootstrap`.

For fully automatic References, runtime progression can be paused/resumed
without pausing the generic job:

- Hospitals: `auto_progress_patient_flow`;
- Logistics: `auto_progress_shipment`;
- Transit: `auto_reconcile_vehicle`;
- Warehouse / Fulfillment: `auto_progress_fulfillment`;
- Hospitality: `auto_progress_reservation`;
- Subscription / SaaS: `auto_progress_plan_change`.

This makes configuration revision observable on the next recurring tick while
preserving the durable world already created at bootstrap.

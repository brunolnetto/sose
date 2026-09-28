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

A running tick must use one resolved configuration snapshot. Configuration
changes take effect at the next orchestration boundary unless a domain models a
business change as durable domain state.

## Rollout

The first contract implementation covers `tutorial_job` and MRO. Remaining
promoted References should receive domain-specific config models incrementally,
with executable tests proving that each advertised parameter changes runtime or
seed behavior.

The rollout should not introduce parameters merely because a constant exists;
only meaningful user-facing configuration belongs in the public domain config.

# Domain DSL

## Purpose

SOSE should make new synthetic operational systems inexpensive to define.

The Domain DSL provides declarative configuration for stable structural aspects of a domain while preserving Python for complex behavior.

The DSL must not become a replacement programming language.

---

## Principle

```text
DSL
    = declaration

Python
    = behavior
```

Use the DSL for stable declarations such as:

```text
entity registration
relationships
StateChart binding
scenario registration
```

As the DSL evolves, it may also describe:

```text
static transition weights
scenario effects
resources
calendar references
source mappings
```

Use Python for:

```text
complex guards
contextual weight functions
custom scenario logic
domain algorithms
derived calculations
integration adapters
```

---

## Current v0.3 loadable schema

The current `load_domain()` implementation validates YAML directly against `DomainSpec` in `src/sose/dsl/models.py`.

The following example is intentionally limited to fields supported by that implementation and can be consumed by the current loader:

```yaml
name: ecommerce

entities:
  - name: customer
    statechart: none
    kind: master

  - name: order
    statechart: ecommerce.statecharts.OrderChart
    kind: stateful

  - name: shipment
    statechart: ecommerce.statecharts.ShipmentChart
    kind: stateful

relationships:
  - name: order_customer
    from_entity: order
    to_entity: customer
    cardinality: many-to-one

  - name: shipment_order
    from_entity: shipment
    to_entity: order
    cardinality: many-to-one

scenarios:
  - name: carrier_disruption
    entity: shipment
    trigger: scheduled
    policy: reduce_carrier_availability
    activation_probability: 0.02
```

At v0.3, the loadable schema contains:

```text
DomainSpec
├── name
├── entities[]
│   ├── name
│   ├── statechart
│   └── kind
├── relationships[]
│   ├── name
│   ├── from_entity
│   ├── to_entity
│   └── cardinality
└── scenarios[]
    ├── name
    ├── entity
    ├── trigger
    ├── policy
    └── activation_probability
```

This section describes the **implemented loader contract**.

The sections below describe the architectural direction of the DSL. Features marked as proposed are not yet guaranteed to be accepted by `load_domain()`.

---

## Entity declarations

The current `EntitySpec` supports:

```text
master
stateful
event
```

Example:

```yaml
entities:
  - name: supplier
    statechart: none
    kind: master

  - name: purchase_order
    statechart: mro.statecharts.PurchaseOrderChart
    kind: stateful
```

A future DSL may add additional entity categories such as resources, but they are not part of the v0.3 `EntitySpec`.

---

## Relationships

Relationships are top-level declarations.

The current representation is:

```yaml
relationships:
  - name: purchase_order_supplier
    from_entity: purchase_order
    to_entity: supplier
    cardinality: many-to-one
```

Supported cardinalities are:

```text
one-to-one
one-to-many
many-to-one
many-to-many
```

Relationships must not be represented both as nested entity properties and as top-level objects. The canonical DSL shape is the top-level list represented by `RelationshipSpec`.

---

## StateChart references

The DSL references StateChart classes rather than duplicating transition topology.

```yaml
- name: work_order
  statechart: mro.statecharts.WorkOrderChart
  kind: stateful
```

The Python StateChart remains the legal behavioral authority.

Future DSL versions may attach stochastic metadata to named states, events, or transitions without redefining the topology itself.

---

## Proposed transition-weight declarations

Static stochastic semantics are a planned DSL extension.

A possible future representation is:

```yaml
probabilistic_transitions:
  order:
    paid:
      ship: 0.97
      cancel: 0.03
```

This structure is **proposed** and is not part of the current `DomainSpec`.

Contextual weights should generally reference Python policies rather than embedding executable expressions in YAML.

A future representation could be:

```yaml
transition_policies:
  order_paid:
    callable: ecommerce.policies.order_paid_weights
```

---

## Current scenario declarations

The current `ScenarioSpec` supports:

```yaml
scenarios:
  - name: carrier_disruption
    entity: shipment
    trigger: scheduled
    policy: reduce_carrier_availability
    activation_probability: 0.02
```

Current trigger values are:

```text
per_tick
on_event
scheduled
```

`activation_probability` describes whether the external scenario/intervention activates.

It does not describe ordinary lifecycle branch probability.

---

## Proposed scenario effects

Composable scenario effects are planned, but are not yet represented by the current `ScenarioSpec`.

A future schema might support:

```yaml
scenarios:
  - name: carrier_disruption
    entity: shipment
    trigger: scheduled
    policy: reduce_carrier_availability
    activation_probability: 0.02

    effects:
      - type: transition_weight_multiplier
        target: shipment.dispatch
        value: 0.4

      - type: delay_multiplier
        target: shipment.delivery_delay
        value: 3.0

    duration: 12h
```

In this proposed example, `shipment` is explicitly declared as an entity and its StateChart is responsible for the referenced behavior.

Before this syntax becomes loadable, the DSL must define how effect targets are validated against StateChart events, policies, resources, and delay models.

---

## Proposed resources

Resources are a planned extension.

A possible future representation is:

```yaml
resources:
  - name: technicians
    type: pool
    capacity: 20

    attributes:
      site: plant-a
```

Possible future capabilities include:

```text
skills
shifts
calendars
locations
capacity schedules
```

This structure is not part of the v0.3 `DomainSpec`.

---

## Proposed calendars

Calendar declarations are also planned:

```yaml
calendars:
  - name: plant_a
    timezone: America/Sao_Paulo
    weekdays: [mon, tue, wed, thu, fri]

    shifts:
      - start: "08:00"
        end: "17:00"
```

This structure is not part of the v0.3 `DomainSpec`.

---

## Proposed source mappings

The DSL may eventually define external source representations.

For example:

```yaml
sources:
  - name: erp
    entities:
      - entity: purchase_order
        table: purchase_order
        expose:
          - id
          - supplier_id
          - state
          - created_at
```

Internal SOSE fields remain excluded unless explicitly mapped.

This structure is not part of the v0.3 `DomainSpec`.

---

## Validation

Validation should evolve in layers.

### Current structural validation

The v0.3 loader validates the Pydantic shape of:

```text
DomainSpec
EntitySpec
RelationshipSpec
ScenarioSpec
```

This includes required fields, entity kinds, relationship cardinalities, scenario trigger values, and the `0.0 <= activation_probability <= 1.0` bound.

### Future reference validation

The DSL should additionally validate semantic references such as:

```text
relationship source entity exists
relationship target entity exists
StateChart import exists
scenario entity exists
resource exists
calendar exists
scenario effect target exists
```

### Future transition validation

For transition-related declarations:

```text
named source state exists
named event exists
transition belongs to StateChart
```

### Future probability validation

For stochastic declarations:

```text
weight >= 0
no NaN
no invalid callable reference
valid policy type
```

Weights do not need to sum to one.

SOSE normalizes enabled transition weights at runtime.

---

## Compilation

The target architecture compiles declarative specifications into runtime objects.

```text
YAML / TOML
    ↓
DomainSpec
    ↓
validation
    ↓
compiled domain
    ├── DomainRegistry
    ├── entity definitions
    ├── StateChart bindings
    ├── transition policies
    ├── scenarios
    ├── resources
    ├── calendars
    └── source mappings
```

At v0.3, `load_domain()` performs YAML parsing plus `DomainSpec.model_validate()`; the richer compilation stage remains future work.

The runtime should eventually operate on compiled domain objects rather than repeatedly interpreting raw YAML.

---

## Versioning

The current `DomainSpec` does not yet expose a `version` field.

Versioning is a planned addition.

A future schema may distinguish:

```text
domain schema version
domain implementation version
simulation configuration version
```

This will support replay and compatibility checks.

Until versioning is implemented in the model, examples intended for `load_domain()` should not present `version` as part of the accepted contract.

---

## Extensibility

The DSL should use explicit extension points rather than arbitrary Python embedded inside YAML.

Preferred direction:

```yaml
policy:
  callable: mro.policies.receipt_delay
```

Avoid:

```yaml
expression: "eval('...')"
```

Arbitrary expression evaluation creates security, validation, and reproducibility problems.

---

## Invariants

1. StateChart topology is not duplicated by the DSL.
2. Implemented examples match the currently loadable schema.
3. Proposed syntax is explicitly identified as future architecture.
4. Relationships use one canonical top-level representation.
5. Transition weights and scenario activation remain distinct concepts.
6. Complex behavior remains implementable in Python.
7. DSL compilation must be deterministic.
8. Runtime behavior must not depend on YAML key-order accidents.
9. Callable references must be explicit and importable.
10. Unknown or unsupported configuration should fail early once strict schema enforcement is enabled.

---

## Future work

- strict unknown-field rejection;
- semantic reference validation;
- transition policy specifications;
- scenario effects;
- resources;
- calendars;
- source mappings;
- explicit schema versioning;
- JSON Schema generation;
- IDE completion;
- DSL migration tooling;
- domain-package discovery;
- plugin system;
- schema compatibility checks;
- visual domain editor.

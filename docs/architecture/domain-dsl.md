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

Use the DSL for:

```text
entity registration
relationships
StateChart binding
static transition weights
scenario declarations
resource declarations
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

## Example

```yaml
domain: ecommerce
version: 1

entities:

  customer:
    kind: master

  order:
    kind: stateful
    statechart: ecommerce.statecharts.OrderChart

    relationships:
      customer:
        target: customer
        cardinality: many-to-one

probabilistic_transitions:

  order:

    paid:
      ship: 0.97
      cancel: 0.03

scenarios:

  carrier_disruption:
    trigger:
      type: periodic
      every: 7d

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

---

## Entity declarations

Possible entity kinds:

```text
master
stateful
event
resource
```

The DSL may define:

```yaml
entities:
  supplier:
    kind: master

  purchase_order:
    kind: stateful
    statechart: mro.statecharts.PurchaseOrderChart
```

---

## Relationships

Relationships should be explicit.

```yaml
relationships:
  supplier:
    target: supplier
    cardinality: many-to-one
    required: true
```

Possible cardinalities:

```text
one-to-one
one-to-many
many-to-one
many-to-many
```

---

## StateChart references

The DSL should reference StateChart classes rather than duplicate transition topology.

```yaml
statechart: mro.statecharts.WorkOrderChart
```

The Python StateChart remains the legal behavioral authority.

The DSL may attach stochastic metadata to named events/transitions.

---

## Transition weights

Static stochastic semantics can be declarative.

```yaml
probabilistic_transitions:

  work_order:

    released:
      start: 0.75
      wait_for_material: 0.20
      cancel: 0.05
```

Contextual weights should typically reference Python policies:

```yaml
policies:
  work_order_released:
    callable: mro.policies.work_order_release_weights
```

---

## Scenario declarations

Scenarios belong naturally in the DSL when their effects are composable.

```yaml
scenarios:

  supplier_disruption:

    trigger:
      type: random
      activation_probability: 0.01

    effects:
      - type: resource_capacity_multiplier
        target: supplier.SUP-123
        value: 0.0

    duration:
      hours: 24
```

Scenario activation probability must remain distinct from transition probability.

---

## Resources

Example:

```yaml
resources:

  technicians:
    type: pool
    capacity: 20

    attributes:
      site: plant-a
```

Future DSL versions may support:

```text
skills
shifts
calendars
locations
capacity schedules
```

---

## Calendars

```yaml
calendars:

  plant_a:
    timezone: America/Sao_Paulo
    weekdays: [mon, tue, wed, thu, fri]

    shifts:
      - start: "08:00"
        end: "17:00"
```

---

## Source mappings

The DSL may eventually define external source representations.

```yaml
sources:

  erp:
    entities:

      purchase_order:
        table: purchase_order

        expose:
          - id
          - supplier_id
          - state
          - created_at
```

Internal SOSE fields remain excluded unless explicitly mapped.

---

## Validation

The DSL loader must validate:

### Structural references

```text
entity exists
StateChart exists
relationship target exists
resource exists
calendar exists
scenario target exists
```

### Transition references

```text
named source state exists
named event exists
transition belongs to StateChart
```

### Probability definitions

```text
weight >= 0
no NaN
no invalid callable reference
valid policy type
```

Weights do not need to sum to one.

SOSE normalizes them.

### Scenario definitions

Validate:

```text
trigger type
duration
effect compatibility
target type
activation probability
```

---

## Compilation

The DSL is compiled into runtime objects.

```text
YAML / TOML
    ↓
DomainSpec
    ↓
validation
    ↓
DomainRegistry
    ├── entity definitions
    ├── StateChart bindings
    ├── transition policies
    ├── scenarios
    ├── resources
    ├── calendars
    └── source mappings
```

The runtime should operate on compiled domain objects rather than repeatedly interpreting raw YAML.

---

## Versioning

Domain definitions should include a version.

```yaml
domain: mro
version: 3
```

Future work may distinguish:

```text
domain schema version
domain implementation version
simulation configuration version
```

This supports replay and compatibility checks.

---

## Extensibility

The DSL should use explicit extension points rather than arbitrary Python embedded inside YAML.

Good:

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
2. DSL declarations are validated before simulation.
3. Transition weights and scenario activation remain distinct.
4. Complex behavior remains implementable in Python.
5. DSL compilation is deterministic.
6. Runtime behavior does not depend on YAML key-order accidents.
7. Callable references are explicit and importable.
8. Unknown configuration fails early.

---

## Future work

- JSON Schema;
- Pydantic models;
- IDE completion;
- DSL migration tooling;
- domain-package discovery;
- plugin system;
- schema compatibility checks;
- visual domain editor.

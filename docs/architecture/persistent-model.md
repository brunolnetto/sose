# SOSE Durable Persistence Model

This document describes the canonical **semantic persistence model** used by SOSE.

It is not a relational database schema. SOSE persistence adapters may store these
records differently. The relationships below are logical relationships encoded by
stable identifiers, names, and durable ownership rules.

Reference-domain specifications should reuse this vocabulary rather than redefining
runtime persistence concepts independently.

## 1. Core lifecycle and recovery model

```mermaid
erDiagram
    ENTITY ||--o{ COMMAND : "targeted by"
    ENTITY ||--o{ DOMAIN_EVENT : "emits"
    COMMAND ||--o| SCHEDULED_WORK : "scheduled as"

    SIMULATION_POSITION ||--|| PERSISTENCE_STATE : "anchors recovery"
    SCENARIO_RUNTIME_STATE ||--|| PERSISTENCE_STATE : "records interventions"

    ENTITY {
        string id
        string entity_type
        string state
        json attributes
        int version
    }

    COMMAND {
        string command_id
        string name
        string entity_type
        string entity_id
        datetime due_at
        string causation_id
        string correlation_id
    }

    DOMAIN_EVENT {
        string event_id
        string name
        string entity_type
        string entity_id
        datetime occurred_at
        string causation_id
        string correlation_id
    }

    SCHEDULED_WORK {
        string work_id
        string command_id
        datetime due_at
        int priority
        int sequence
    }

    SIMULATION_POSITION {
        datetime logical_time
        int logical_tick
        int execution_sequence
        int committed_sequence
    }

    SCENARIO_RUNTIME_STATE {
        json decisions
        json active_effects
    }

    PERSISTENCE_STATE {
        string conceptual_root
    }
```

The synthetic `PERSISTENCE_STATE` node is diagram-only. It represents the durable
snapshot owned by an adapter; it is not a runtime dataclass.

## 2. Resource persistence

### 2.1 Non-preemptive resources

```mermaid
erDiagram
    RESOURCE_DEFINITION ||--o{ RESOURCE_DEMAND : "receives"
    RESOURCE_DEFINITION ||--o{ RESOURCE_RESERVATION : "owns"
    RESOURCE_DEMAND ||--o| RESOURCE_RESERVATION : "becomes"
    RESOURCE_RESERVATION ||--o| RESOURCE_RELEASE_INTENT : "released by"

    RESOURCE_DEFINITION {
        string name
        int capacity
    }

    RESOURCE_DEMAND {
        string request_id
        string resource_name
        int priority
        int sequence
        datetime requested_at
    }

    RESOURCE_RESERVATION {
        string reservation_id
        string request_id
        string resource_name
        datetime acquired_at
        int sequence
    }

    RESOURCE_RELEASE_INTENT {
        string intent_id
        string reservation_id
        string resource_name
        datetime requested_at
    }
```

A request is represented by a demand while pending and by a reservation after
acquisition. Backend-native request objects are not durable truth.

### 2.2 Preemptive resources

```mermaid
erDiagram
    PREEMPTIVE_RESOURCE_DEFINITION ||--o{ PREEMPTIVE_RESOURCE_DEMAND : "receives"
    PREEMPTIVE_RESOURCE_DEFINITION ||--o{ PREEMPTIVE_RESOURCE_RESERVATION : "owns"
    PREEMPTIVE_RESOURCE_DEMAND ||--o| PREEMPTIVE_RESOURCE_RESERVATION : "becomes"
    PREEMPTIVE_RESOURCE_RESERVATION ||--o| PREEMPTIVE_RESOURCE_RELEASE_INTENT : "released by"
    PREEMPTIVE_RESOURCE_RESERVATION ||--o{ RESOURCE_PREEMPTION_RESULT : "may be displaced"
    PREEMPTIVE_RESOURCE_RESERVATION ||--o{ RESOURCE_PREEMPTION_RESULT : "may succeed"

    PREEMPTIVE_RESOURCE_DEFINITION {
        string name
        int capacity
    }

    PREEMPTIVE_RESOURCE_DEMAND {
        string request_id
        string resource_name
        int priority
        boolean preempt
        int sequence
        datetime requested_at
    }

    PREEMPTIVE_RESOURCE_RESERVATION {
        string reservation_id
        string request_id
        string resource_name
        int priority
        int sequence
        datetime acquired_at
    }

    PREEMPTIVE_RESOURCE_RELEASE_INTENT {
        string intent_id
        string reservation_id
        string resource_name
        datetime requested_at
    }

    RESOURCE_PREEMPTION_RESULT {
        string result_id
        string resource_name
        string displaced_reservation_id
        string displaced_request_id
        string preempting_request_id
        string successor_reservation_id
        datetime preempted_at
        int sequence
    }
```

`ResourcePreemptionResult` is immutable evidence of displacement. Historical results
remain durable after the active reservation changes.

## 3. Store persistence

```mermaid
erDiagram
    STORE_DEFINITION ||--o{ DURABLE_STORE_ITEM : "contains"
    STORE_DEFINITION ||--o{ STORE_PUT_INTENT : "accepts"
    STORE_DEFINITION ||--o{ STORE_GET_REQUEST : "serves"
    STORE_GET_REQUEST ||--o| STORE_GET_RESULT : "completes as"
    DURABLE_STORE_ITEM ||--o| STORE_GET_RESULT : "returned as"

    STORE_DEFINITION {
        string name
        string kind
        int capacity
    }

    DURABLE_STORE_ITEM {
        string item_id
        string store_name
        json value
        int priority
        int sequence
    }

    STORE_PUT_INTENT {
        string item_id
        string store_name
        json value
        datetime requested_at
        int sequence
    }

    STORE_GET_REQUEST {
        string request_id
        string store_name
        datetime requested_at
        int sequence
        string filter_key
    }

    STORE_GET_RESULT {
        string request_id
        string store_name
        json item
        datetime completed_at
        int sequence
    }
```

Store records represent discrete identity: lots, WIP objects, receipts, or other
individually meaningful items.

## 4. Container persistence

```mermaid
erDiagram
    CONTAINER_DEFINITION ||--|| CONTAINER_STATE : "defines"
    CONTAINER_DEFINITION ||--o{ CONTAINER_OPERATION_INTENT : "receives"
    CONTAINER_OPERATION_INTENT ||--o| CONTAINER_OPERATION_RESULT : "completes as"

    CONTAINER_DEFINITION {
        string name
        float capacity
        float initial
    }

    CONTAINER_STATE {
        string name
        float level
    }

    CONTAINER_OPERATION_INTENT {
        string request_id
        string container_name
        string operation
        float amount
        datetime requested_at
        int sequence
    }

    CONTAINER_OPERATION_RESULT {
        string request_id
        string container_name
        string operation
        float amount
        float level_before
        float level_after
        datetime completed_at
        int sequence
    }
```

Containers represent quantitative balance. A domain may deliberately combine a Store
and a Container when it needs both discrete identity and aggregate quantity; such a
multi-representation operation must establish feasibility before partially executing
one side.

## 5. Persistence ownership boundary

The durable model owns semantic facts needed to reconstruct execution:

- entity lifecycle and attributes;
- commands and immutable domain events;
- future scheduled work;
- logical simulation position;
- scenario runtime state;
- resource demands, reservations, releases, and preemption evidence;
- Store definitions/items/intents/requests/results;
- Container definitions/state/intents/results.

The following remain ephemeral and reconstructible:

- SimPy environments;
- backend event/request objects;
- callbacks;
- process/generator continuations;
- backend queues;
- resource handles.

Reference-domain ERDs should show only the relevant subset of this model and explain
how domain entities interpret those durable records.

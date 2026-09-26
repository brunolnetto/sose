# Procure-to-Pay flow

## Happy path

```mermaid
flowchart TD
    A["Material need"] --> B["Requisition(requested)"]
    B -->|approve| C["Requisition(approved)"]
    C -->|order| D["Requisition(ordered)"]
    D --> E["PurchaseOrder(created)"]
    E -->|submit| F["PurchaseOrder(submitted)"]
    F -->|confirm| G["PurchaseOrder(confirmed)"]
    G -->|durable supplier lead time| H["PurchaseOrder(in_transit)"]
    H -->|scheduled receive| I["PurchaseOrder(received)"]
    I --> J["Receipt(pending)"]
    J -->|receiving resource| K["Receipt(receiving)"]
    K -->|inspect| L["Receipt(inspected)"]
    L -->|stock| M["Receipt(stocked)"]
    M --> N["Inventory Store / Container"]
    N --> O["MaterialDemand(allocated)"]
    O --> P["MaterialDemand(consumed)"]
```

## Contention path

Receiving capacity is intentionally finite.

```mermaid
flowchart LR
    A["Receipt A"] --> Q["Receiving queue"]
    B["Receipt B"] --> Q
    C["Receipt C"] --> Q
    Q --> D["Dock / inspector"]
    D --> E["Stock"]
```

The queue and resource mechanics may be backend-driven, but demand and reservation
semantics must survive restart.

## Shortage path

```mermaid
flowchart TD
    A["MaterialDemand(open)"] -->|request inventory| B{"Stock sufficient?"}
    B -->|no| C["waiting_inventory / backordered"]
    C -->|replenishment arrives| D["inventory available"]
    D --> E["allocated"]
    E --> F["consumed"]
```

The pending demand must not disappear across restart, and replenishment must satisfy it
exactly once.

## Recovery boundaries to test

At minimum:

1. after requisition approval, before PO execution;
2. while supplier lead-time work is still scheduled;
3. with a receipt waiting for constrained receiving capacity;
4. with a pending Store or Container operation;
5. while material demand is waiting for replenishment;
6. after scenario activation but before its effects have expired.

For each boundary, the final durable snapshot must match continuous execution.

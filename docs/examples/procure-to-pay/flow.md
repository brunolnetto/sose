# Procure-to-Pay flow

## Happy path

```text
Material need
    ↓
Requisition(requested)
    ↓ approve
Requisition(approved)
    ↓ order
Requisition(ordered)
    ↓
PurchaseOrder(created)
    ↓ submit
PurchaseOrder(submitted)
    ↓ confirm
PurchaseOrder(confirmed)
    ↓ durable supplier lead time
PurchaseOrder(in_transit)
    ↓ scheduled receive
PurchaseOrder(received)
    ↓
Receipt(pending)
    ↓ receiving resource
Receipt(receiving)
    ↓ inspect
Receipt(inspected)
    ↓ stock
Receipt(stocked)
    ↓
Inventory Store / Container
    ↓
MaterialDemand(allocation)
    ↓
MaterialDemand(consumed)
```

## Contention path

Receiving capacity is intentionally finite.

```text
receipt A ─┐
receipt B ─┼→ receiving queue → dock / inspector → stock
receipt C ─┘
```

The queue and resource mechanics may be backend-driven, but demand and reservation
semantics must survive restart.

## Shortage path

```text
MaterialDemand(open)
    ↓ request inventory
insufficient stock
    ↓
waiting_inventory / backordered
    ↓ replenishment arrives
inventory becomes available
    ↓
allocated
    ↓
consumed
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

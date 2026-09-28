# Warehouse / Fulfillment — Executable Specification

## 1. Purpose

Test whether SOSE can distinguish inventory ownership, current physical
projection, and immutable movement evidence without introducing a generic
inventory ledger primitive prematurely.

## 2. Durable entities

FulfillmentOrder lifecycle:
requested -> allocated -> picking -> packed -> shipped.

Allocation lifecycle:
committed -> picked -> shipped, with committed -> released available for future
cancellation breadth.

InventoryLot stores SKU, on_hand, allocated, and occurrence IDs. It is a
mutable projection rather than historical evidence.

InventoryOccurrence lifecycle:
captured -> committed.

## 3. Allocation semantics

Allocation computes a complete plan before persistence. If eligible inventory
cannot satisfy the order, no partial allocations or allocated balances are
committed.

Primary SKU inventory is preferred. Explicitly acceptable substitute SKUs may
satisfy the remainder. Allocation increments lot allocated quantity but does not
change physical on_hand.

## 4. Picking semantics

Each Allocation has one deterministic pick occurrence. The first execution:

1. decrements lot on_hand;
2. decrements lot allocated;
3. persists captured immutable occurrence in the same transaction;
4. commits occurrence;
5. moves Allocation to picked.

If execution stops after step 3, retry/restart observes the existing occurrence
and resumes state transitions without applying the physical decrement again.

## 5. Corrections

A correction has deterministic identity (lot, sequence), delta, and resulting
on-hand quantity. Reusing its identity with a different delta is illegal.

A correction may not drive on-hand below already allocated stock.

## 6. Invariants

WH-01 — Allocation ownership and physical on-hand are distinct durable truths.

WH-02 — Insufficient inventory cannot leave partial allocation ownership.

WH-03 — Substitute use is explicit on Allocation evidence.

WH-04 — Physical on-hand changes only when picking or correction evidence is
persisted.

WH-05 — Pick occurrence identity prevents duplicate decrement on retry/restart.

WH-06 — Corrections append evidence; prior occurrences remain immutable.

WH-07 — Packing requires every Allocation to be picked.

WH-08 — Restart after a partial pick continues remaining work without replaying
committed movement.

## 7. Happy path

Allocate 10 units across primary and substitute lots, pick both allocations,
pack, ship, and verify final lot projections plus immutable occurrence indexes.

## 8. Representative sad paths

- insufficient total inventory;
- pack before every allocation is picked;
- conflicting replay of a correction identity;
- correction below already allocated quantity.

## 9. Restart equivalence

Commit the first pick, rebuild the backend/runtime, finish the second pick,
pack and ship. The first lot must not be decremented twice.

## 10. Executable evidence

| Requirement | Evidence |
| --- | --- |
| statecharts | test_warehouse_fulfillment_statecharts.py |
| happy path / substitution | test_warehouse_fulfillment_happy_path.py |
| sad paths / corrections | test_warehouse_fulfillment_sad_paths.py |
| restart equivalence | test_warehouse_fulfillment_restart_equivalence.py |
| immutable occurrences | happy/sad/restart suites |

## 11. Promotion decision

Current status: **Reference implementation**.

Do not extract a generic inventory-ledger abstraction until another materially
different domain repeats the same ownership/projection/occurrence contract.

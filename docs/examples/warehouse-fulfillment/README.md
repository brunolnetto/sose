# Warehouse / Fulfillment Reference

## Status

**Reference implementation.**

This Reference is grounded in GS1 EPCIS 2.0's event-oriented visibility model:
operational projections can change, while durable events preserve what happened,
where, when, and in which business context.

The executable slice tests three truths that must not collapse into one field:

- Allocation owns promised inventory;
- InventoryLot is the mutable physical projection;
- InventoryOccurrence is immutable movement/correction evidence.

## Executable slice

One order requests 10 units of widget-a. The warehouse has:

- 6 units of widget-a in a primary lot;
- 5 units of an allowed substitute, widget-b.

Allocation plans the whole request before committing anything, consumes the
primary SKU first, and then allocates 4 substitute units. Allocation does not
decrement on-hand inventory.

Picking decrements physical on-hand projection and committed allocation, while
creating one immutable pick occurrence per allocation. Packing and shipping add
their own occurrence evidence.

An inventory correction changes the lot projection by appending a correction
occurrence. It never rewrites prior pick/correction history.

## Restart pressure

A partial pick may commit one lot movement while another allocation remains
unpicked. Rebuild must continue from the durable Allocation and occurrence
states without decrementing the already-picked lot again.

## Stopping rule

The Reference stops after proving atomic allocation planning, substitution,
projection-versus-ownership separation, immutable movement/correction evidence,
idempotent partial-pick recovery, and restart equivalence.

Wave planning, routing, cartons, carrier tendering, replenishment, cycle-count
workflows, and advanced slotting remain breadth until they expose a distinct
SOSE architectural question.

## Reference

- GS1 EPCIS 2.0 standard and implementation guidance for event-based
  supply-chain visibility.

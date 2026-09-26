from statemachine import State, StateChart


class RequisitionChart(StateChart):
    requested = State(initial=True)
    approved = State()
    ordered = State(final=True)
    rejected = State(final=True)

    approve = requested.to(approved)
    reject = requested.to(rejected)
    order = approved.to(ordered)


class PurchaseOrderChart(StateChart):
    created = State(initial=True)
    submitted = State()
    confirmed = State()
    in_transit = State()
    delayed = State()
    received = State()
    closed = State(final=True)
    cancelled = State(final=True)

    submit = created.to(submitted)
    confirm = submitted.to(confirmed)
    dispatch = confirmed.to(in_transit) | delayed.to(in_transit)
    mark_delayed = confirmed.to(delayed) | in_transit.to(delayed)
    receive = in_transit.to(received)
    close = received.to(closed)
    cancel = created.to(cancelled) | submitted.to(cancelled) | confirmed.to(cancelled)


class ReceiptChart(StateChart):
    pending = State(initial=True)
    receiving = State()
    partial = State()
    inspected = State()
    stocked = State(final=True)
    rejected = State(final=True)

    begin_receiving = pending.to(receiving)
    mark_partial = receiving.to(partial)
    inspect = receiving.to(inspected) | partial.to(inspected)
    reject = receiving.to(rejected) | partial.to(rejected) | inspected.to(rejected)
    stock = inspected.to(stocked)


class MaterialDemandChart(StateChart):
    open = State(initial=True)
    waiting_inventory = State()
    backordered = State()
    allocated = State()
    consumed = State(final=True)
    cancelled = State(final=True)

    wait_for_inventory = open.to(waiting_inventory)
    backorder = open.to(backordered) | waiting_inventory.to(backordered)
    allocate = (
        open.to(allocated)
        | waiting_inventory.to(allocated)
        | backordered.to(allocated)
    )
    consume = allocated.to(consumed)
    cancel = (
        open.to(cancelled)
        | waiting_inventory.to(cancelled)
        | backordered.to(cancelled)
    )

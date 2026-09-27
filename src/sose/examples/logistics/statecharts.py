from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


class ShipmentChart(StateChart):
    created = State(initial=True)
    pickup_scheduled = State()
    picked_up = State()
    at_origin_hub = State()
    in_transfer = State()
    at_destination_hub = State()
    out_for_delivery = State()
    delayed = State()
    delivered = State(final=True)
    lost = State(final=True)
    damaged = State(final=True)
    returned = State(final=True)

    schedule_pickup = created.to(pickup_scheduled)
    pickup = pickup_scheduled.to(picked_up)
    arrive_origin_hub = picked_up.to(at_origin_hub)
    dispatch_transfer = at_origin_hub.to(in_transfer)
    arrive_destination_hub = in_transfer.to(at_destination_hub)
    dispatch_delivery = at_destination_hub.to(out_for_delivery)
    deliver = out_for_delivery.to(delivered)

    delay = (
        pickup_scheduled.to(delayed)
        | picked_up.to(delayed)
        | at_origin_hub.to(delayed)
        | in_transfer.to(delayed)
        | at_destination_hub.to(delayed)
        | out_for_delivery.to(delayed)
    )
    resume_pickup = delayed.to(pickup_scheduled)
    resume_transfer = delayed.to(in_transfer)
    resume_delivery = delayed.to(out_for_delivery)

    mark_lost = (
        picked_up.to(lost)
        | at_origin_hub.to(lost)
        | in_transfer.to(lost)
        | at_destination_hub.to(lost)
        | out_for_delivery.to(lost)
    )
    mark_damaged = (
        picked_up.to(damaged)
        | at_origin_hub.to(damaged)
        | in_transfer.to(damaged)
        | at_destination_hub.to(damaged)
        | out_for_delivery.to(damaged)
    )
    return_to_sender = out_for_delivery.to(returned)


@probabilistic_transitions(
    {"deliver": 0.85, "fail": 0.15},
    excluded_events={"dispatch", "schedule_retry", "retry", "exhaust"},
)
class DeliveryAttemptChart(StateChart):
    pending = State(initial=True)
    out_for_delivery = State()
    failed = State()
    retry_scheduled = State()
    delivered = State(final=True)
    exhausted = State(final=True)

    dispatch = pending.to(out_for_delivery)
    deliver = out_for_delivery.to(delivered)
    fail = out_for_delivery.to(failed)
    schedule_retry = failed.to(retry_scheduled)
    retry = retry_scheduled.to(out_for_delivery)
    exhaust = failed.to(exhausted)

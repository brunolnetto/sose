from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "schedule_pickup",
        "pickup",
        "arrive_origin_hub",
        "dispatch_transfer",
        "arrive_destination_hub",
        "dispatch_delivery",
        "deliver",
        "delay",
        "resume",
        "mark_lost",
        "mark_damaged",
        "return_to_sender",
    },
)
class ShipmentChart(StateChart):
    created = State(initial=True)
    pickup_scheduled = State()
    picked_up = State()
    at_origin_hub = State()
    in_transfer = State()
    at_destination_hub = State()
    out_for_delivery = State()

    delayed_pickup = State()
    delayed_after_pickup = State()
    delayed_origin_hub = State()
    delayed_transfer = State()
    delayed_destination_hub = State()
    delayed_delivery = State()

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
        pickup_scheduled.to(delayed_pickup)
        | picked_up.to(delayed_after_pickup)
        | at_origin_hub.to(delayed_origin_hub)
        | in_transfer.to(delayed_transfer)
        | at_destination_hub.to(delayed_destination_hub)
        | out_for_delivery.to(delayed_delivery)
    )
    resume = (
        delayed_pickup.to(pickup_scheduled)
        | delayed_after_pickup.to(picked_up)
        | delayed_origin_hub.to(at_origin_hub)
        | delayed_transfer.to(in_transfer)
        | delayed_destination_hub.to(at_destination_hub)
        | delayed_delivery.to(out_for_delivery)
    )

    mark_lost = (
        picked_up.to(lost)
        | delayed_after_pickup.to(lost)
        | at_origin_hub.to(lost)
        | delayed_origin_hub.to(lost)
        | in_transfer.to(lost)
        | delayed_transfer.to(lost)
        | at_destination_hub.to(lost)
        | delayed_destination_hub.to(lost)
        | out_for_delivery.to(lost)
        | delayed_delivery.to(lost)
    )
    mark_damaged = (
        picked_up.to(damaged)
        | delayed_after_pickup.to(damaged)
        | at_origin_hub.to(damaged)
        | delayed_origin_hub.to(damaged)
        | in_transfer.to(damaged)
        | delayed_transfer.to(damaged)
        | at_destination_hub.to(damaged)
        | delayed_destination_hub.to(damaged)
        | out_for_delivery.to(damaged)
        | delayed_delivery.to(damaged)
    )
    return_to_sender = (
        out_for_delivery.to(returned)
        | delayed_delivery.to(returned)
    )


@probabilistic_transitions(
    {"deliver": 0.85, "fail": 0.15},
    excluded_events={"dispatch"},
)
class DeliveryAttemptChart(StateChart):
    pending = State(initial=True)
    out_for_delivery = State()
    delivered = State(final=True)
    failed = State(final=True)

    dispatch = pending.to(out_for_delivery)
    deliver = out_for_delivery.to(delivered)
    fail = out_for_delivery.to(failed)

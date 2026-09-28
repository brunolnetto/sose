from datetime import timedelta

from sose.examples.catalog import builtin_catalog
from sose.persistence.memory import MemoryPersistence


def _seed(name: str, parameters: dict[str, object]):
    definition = builtin_catalog().get(name)
    config = definition.parse_config(parameters)
    persistence = MemoryPersistence()
    seeded = definition.seed(persistence, config)
    return persistence, config, seeded


def test_transit_operational_parameters_change_schedule_and_vehicle():
    persistence, config, seeded = _seed(
        "transit",
        {
            "vehicle_key": "vehicle-custom",
            "block_id": "block-custom",
            "first_trip_delay": timedelta(minutes=20),
            "trip_duration": timedelta(minutes=45),
            "layover": timedelta(minutes=5),
        },
    )
    vehicle = persistence.entity("transit_vehicle", seeded.vehicle_id)
    trip_a = persistence.entity("transit_scheduled_trip", seeded.trip_a_id)
    trip_b = persistence.entity("transit_scheduled_trip", seeded.trip_b_id)

    assert vehicle.attributes["vehicle_key"] == "vehicle-custom"
    assert vehicle.attributes["block_id"] == "block-custom"
    assert trip_a.attributes["scheduled_start_at"] == (
        config.start_at + timedelta(minutes=20)
    ).isoformat()
    assert trip_b.attributes["scheduled_start_at"] == (
        config.start_at + timedelta(minutes=20 + 45 + 5)
    ).isoformat()


def test_telecom_operational_parameters_change_product_and_capacity():
    persistence, _, seeded = _seed(
        "telecom",
        {
            "customer_id": "customer-x",
            "product_offering": "enterprise-fixed",
            "access_technology": "fiber",
            "sim_type": "none",
            "provisioning_capacity": 4,
        },
    )
    order = persistence.entity("telecom_product_order", seeded.product_order_id)

    assert order.attributes["customer_id"] == "customer-x"
    assert order.attributes["product_offering"] == "enterprise-fixed"
    assert order.attributes["access_technology"] == "fiber"
    assert persistence.resource_definitions()[0].capacity == 4


def test_aviation_operational_parameters_change_rotation_and_capacities():
    persistence, _, seeded = _seed(
        "aviation",
        {
            "tail_number": "PT-SOSE",
            "leg1_number": "SO100",
            "leg2_number": "SO101",
            "flight_crew_capacity": 3,
            "inspection_team_capacity": 2,
            "maintenance_bay_capacity": 2,
            "maintenance_queue_capacity": 25,
            "part_lot_capacity": 30,
        },
    )
    aircraft = persistence.entity("aviation_aircraft", seeded.aircraft_id)
    resources = {item.name: item.capacity for item in persistence.resource_definitions()}
    preemptive = {
        item.name: item.capacity
        for item in persistence.preemptive_resource_definitions()
    }
    stores = {item.name: item.capacity for item in persistence.store_definitions()}

    assert aircraft.attributes["tail_number"] == "PT-SOSE"
    assert aircraft.attributes["rotation"] == "SO100/SO101"
    assert resources["flight_crew"] == 3
    assert resources["inspection_team"] == 2
    assert preemptive["maintenance_bay"] == 2
    assert stores["maintenance_queue"] == 25
    assert stores["part_lots"] == 30


def test_logistics_operational_parameters_change_route_capacity_and_due_time():
    persistence, config, seeded = _seed(
        "logistics",
        {
            "service_level": "express",
            "route": "A:B:C",
            "resource_capacity": 2,
            "hub_queue_capacity": 40,
            "pickup_delay": timedelta(minutes=30),
        },
    )
    shipment = persistence.entity("shipment", seeded.shipment_id)

    assert shipment.attributes["service_level"] == "express"
    assert shipment.attributes["route"] == "A:B:C"
    assert all(item.capacity == 2 for item in persistence.resource_definitions())
    assert all(item.capacity == 40 for item in persistence.store_definitions())
    assert persistence.scheduled_work()[0].due_at == (
        config.start_at + timedelta(minutes=30)
    )


def test_hospitality_operational_parameters_change_inventory():
    persistence, _, seeded = _seed(
        "hospitality",
        {"room_count": 4, "room_type": "suite"},
    )
    hotel = persistence.entity("hospitality_hotel", seeded.hotel_id)

    assert len(seeded.room_ids) == 4
    assert len(hotel.attributes["room_ids"]) == 4
    for room_id in seeded.room_ids:
        room = persistence.entity("hospitality_room", room_id)
        assert room.attributes["room_type"] == "suite"


def test_field_service_operational_parameters_change_requirements_and_capacity():
    persistence, _, seeded = _seed(
        "field_service",
        {
            "territory": "north",
            "required_skill": "hvac",
            "wrong_skill": "plumbing",
            "technician_resource_capacity": 2,
            "parts_store_capacity": 50,
        },
    )
    work_order = persistence.entity("field_work_order", seeded.work_order_id)

    assert work_order.attributes["territory"] == "north"
    assert work_order.attributes["required_skills"] == ["hvac"]
    assert all(item.capacity == 2 for item in persistence.resource_definitions())
    assert persistence.store_definitions()[0].capacity == 50


def test_cards_payments_operational_parameters_change_value_and_capacity():
    persistence, _, seeded = _seed(
        "cards_payments",
        {"amount": 999.5, "currency": "BRL", "processor_capacity": 3},
    )
    payment = persistence.entity("card_payment", seeded.payment_id)

    assert payment.attributes["amount"] == 999.5
    assert payment.attributes["currency"] == "BRL"
    assert all(item.capacity == 3 for item in persistence.resource_definitions())


def test_energy_operational_parameters_change_meter_topology():
    persistence, _, seeded = _seed(
        "energy_utilities",
        {
            "primary_customer_id": "utility-customer",
            "include_secondary": False,
            "quantity_kind": "power",
            "unit": "kW",
        },
    )
    point = persistence.entity("utility_service_point", seeded.service_point_id)
    meter = persistence.entity("utility_meter", seeded.meter_id)

    assert point.attributes["customer_id"] == "utility-customer"
    assert meter.attributes["quantity_kind"] == "power"
    assert meter.attributes["unit"] == "kW"
    assert seeded.secondary_service_point_id is None
    assert seeded.secondary_meter_id is None


def test_subscription_operational_parameters_change_initial_contract():
    persistence, config, seeded = _seed(
        "subscription_saas",
        {
            "customer_id": "tenant-42",
            "initial_plan": "enterprise",
            "term_duration": timedelta(days=90),
        },
    )
    subscription = persistence.entity("saas_subscription", seeded.subscription_id)

    assert subscription.attributes["customer_id"] == "tenant-42"
    assert subscription.attributes["plan_code"] == "enterprise"
    assert subscription.attributes["term_end_at"] == (
        config.start_at + timedelta(days=90)
    ).isoformat()


def test_warehouse_operational_parameters_change_demand_and_supply():
    persistence, _, seeded = _seed(
        "warehouse_fulfillment",
        {
            "requested_quantity": 7.0,
            "primary_on_hand": 2.0,
            "substitute_on_hand": 8.0,
            "allow_substitute": False,
        },
    )
    order = persistence.entity("warehouse_fulfillment_order", seeded.order_id)
    lots = [
        persistence.entity("warehouse_inventory_lot", lot_id)
        for lot_id in seeded.lot_ids
    ]

    assert order.attributes["requested_quantity"] == 7.0
    assert order.attributes["acceptable_skus"] == ["widget-a"]
    assert [lot.attributes["on_hand"] for lot in lots] == [2.0, 8.0]

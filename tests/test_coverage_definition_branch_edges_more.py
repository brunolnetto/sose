from __future__ import annotations

from types import SimpleNamespace

from sose.examples.airports import definition as airports_definition
from sose.examples.energy_utilities import definition as energy_definition
from sose.examples.field_service import definition as field_service_definition
from sose.examples.manufacturing import definition as manufacturing_definition
from sose.examples.manufacturing import scenarios as manufacturing_scenarios
from sose.examples.order_to_cash import definition as order_to_cash_definition
from sose.examples.warehouse_fulfillment import definition as warehouse_definition


class _EntityPersistence:
    def __init__(self, entities: dict[tuple[str, str], object | None]):
        self._entities = entities

    def entity(self, kind: str, entity_id: str):
        return self._entities.get((kind, entity_id))


def _default_config(definition, **updates):
    return definition.definition.default_config().model_copy(update=updates)


def test_airports_reconcile_gate_stage_returns_not_progressed_on_capacity_block(monkeypatch):
    turnaround = SimpleNamespace(state="arrived")
    monkeypatch.setattr(airports_definition, "reconcile_gate", lambda *args, **kwargs: False)

    refreshed, progressed = airports_definition._reconcile_gate_stage(
        object(),
        object(),
        object(),
        entities=SimpleNamespace(),
        turnaround=turnaround,
    )

    assert refreshed is turnaround
    assert progressed is False


def test_airports_reconcile_tick_routes_scheduled_and_waiting_slot_branches(monkeypatch):
    entities = SimpleNamespace(turnaround_id="T1")
    scheduled = SimpleNamespace(state="scheduled")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): scheduled})
    calls: list[str] = []
    monkeypatch.setattr(
        airports_definition,
        "schedule_arrival",
        lambda *args, **kwargs: calls.append("arrival"),
    )
    monkeypatch.setattr(
        airports_definition,
        "schedule_departure_slot",
        lambda *args, **kwargs: calls.append("slot"),
    )

    airports_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        airports_definition.definition.default_config(),
        entities,
    )

    assert calls == ["arrival", "slot"]

    waiting_slot = SimpleNamespace(state="waiting_slot")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): waiting_slot})
    calls.clear()
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_gate_stage",
        lambda *args, **kwargs: (waiting_slot, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_ground_stage",
        lambda *args, **kwargs: (waiting_slot, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_baggage_stage",
        lambda *args, **kwargs: (waiting_slot, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "reconcile_departure",
        lambda *args, **kwargs: calls.append("depart"),
    )

    airports_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        airports_definition.definition.default_config(),
        entities,
    )

    assert calls == ["depart"]


def test_airports_reconcile_tick_stops_when_gate_stage_not_progressed(monkeypatch):
    entities = SimpleNamespace(turnaround_id="T1")
    ready = SimpleNamespace(state="arrived")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): ready})
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_gate_stage",
        lambda *args, **kwargs: (ready, False),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_ground_stage",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ground stage should not run when gate stage blocks")
        ),
    )

    airports_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        airports_definition.definition.default_config(),
        entities,
    )


def test_airports_ground_baggage_and_departed_guard_paths(monkeypatch):
    entities = SimpleNamespace(turnaround_id="T1")
    turnaround = SimpleNamespace(state="gate_assigned")
    monkeypatch.setattr(
        airports_definition,
        "reconcile_ground_service",
        lambda *args, **kwargs: False,
    )
    refreshed, progressed = airports_definition._reconcile_ground_stage(
        object(),
        object(),
        object(),
        entities=entities,
        turnaround=turnaround,
    )
    assert refreshed is turnaround
    assert progressed is False

    turnaround = SimpleNamespace(state="boarding")
    monkeypatch.setattr(
        airports_definition,
        "reconcile_baggage",
        lambda *args, **kwargs: False,
    )
    refreshed, progressed = airports_definition._reconcile_baggage_stage(
        object(),
        object(),
        entities=entities,
        turnaround=turnaround,
    )
    assert refreshed is turnaround
    assert progressed is False

    departed = SimpleNamespace(state="departed")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): departed})
    monkeypatch.setattr(
        airports_definition,
        "schedule_arrival",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("schedule_arrival should not run for departed turnaround")
        ),
    )
    airports_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        airports_definition.definition.default_config(),
        entities,
    )

    arrived = SimpleNamespace(state="arrived")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): arrived})
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_gate_stage",
        lambda *args, **kwargs: (arrived, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_ground_stage",
        lambda *args, **kwargs: (arrived, False),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_baggage_stage",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("baggage stage should not run when ground stage blocks")
        ),
    )
    airports_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        airports_definition.definition.default_config(),
        entities,
    )

    waiting_baggage = SimpleNamespace(state="waiting_baggage")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): waiting_baggage})
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_ground_stage",
        lambda *args, **kwargs: (waiting_baggage, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_baggage_stage",
        lambda *args, **kwargs: (waiting_baggage, False),
    )
    airports_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        airports_definition.definition.default_config(),
        entities,
    )


def test_field_service_active_appointment_and_start_guard_paths(monkeypatch):
    entities = SimpleNamespace(work_order_id="WO1")
    work_order = SimpleNamespace(
        state="ready",
        attributes={"active_appointment_id": None, "appointment_ids": []},
    )
    persistence = _EntityPersistence({("field_work_order", "WO1"): work_order})

    active_order, active_appointment = field_service_definition._active_appointment(
        persistence,
        entities=entities,
    )
    assert active_order is work_order
    assert active_appointment is None

    _, _, should_continue = field_service_definition._reconcile_work_start_if_ready(
        persistence,
        object(),
        object(),
        entities=entities,
        work_order=None,
        appointment=SimpleNamespace(id="A1", state="in_progress"),
    )
    assert should_continue is False

    appointment = SimpleNamespace(id="A1", state="in_progress")
    scheduled_order = SimpleNamespace(state="scheduled", attributes={})
    monkeypatch.setattr(
        field_service_definition,
        "reconcile_work_start",
        lambda *args, **kwargs: False,
    )
    _, _, should_continue = field_service_definition._reconcile_work_start_if_ready(
        persistence,
        object(),
        object(),
        entities=entities,
        work_order=scheduled_order,
        appointment=appointment,
    )
    assert should_continue is False


def test_field_service_reconcile_tick_returns_on_completed_or_no_appointment(monkeypatch):
    entities = SimpleNamespace(work_order_id="WO1")
    completed = SimpleNamespace(state="completed", attributes={"active_appointment_id": None})
    persistence = _EntityPersistence({("field_work_order", "WO1"): completed})
    monkeypatch.setattr(
        field_service_definition,
        "seed_part_inventory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("seed_part_inventory should not run for completed work")
        ),
    )

    field_service_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        _default_config(field_service_definition, auto_seed_required_part=True),
        entities,
    )

    scheduled = SimpleNamespace(
        state="scheduled",
        attributes={"active_appointment_id": None},
    )
    persistence = _EntityPersistence({("field_work_order", "WO1"): scheduled})
    monkeypatch.setattr(
        field_service_definition,
        "_ensure_active_appointment",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        field_service_definition,
        "_active_appointment",
        lambda *args, **kwargs: (scheduled, None),
    )
    field_service_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        _default_config(field_service_definition, auto_seed_required_part=False),
        entities,
    )


def test_field_service_reconcile_tick_stops_when_start_reconcile_blocks(monkeypatch):
    entities = SimpleNamespace(work_order_id="WO1")
    work_order = SimpleNamespace(state="scheduled", attributes={"active_appointment_id": "A1"})
    appointment = SimpleNamespace(id="A1", state="in_progress")
    persistence = _EntityPersistence(
        {
            ("field_work_order", "WO1"): work_order,
            ("field_appointment", "A1"): appointment,
        }
    )
    monkeypatch.setattr(field_service_definition, "_ensure_active_appointment", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        field_service_definition,
        "_active_appointment",
        lambda *args, **kwargs: (work_order, appointment),
    )
    monkeypatch.setattr(
        field_service_definition,
        "_reconcile_work_start_if_ready",
        lambda *args, **kwargs: (work_order, appointment, False),
    )
    monkeypatch.setattr(
        field_service_definition,
        "record_visit",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("record_visit should not run when start reconcile blocks")
        ),
    )

    field_service_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        _default_config(field_service_definition, auto_seed_required_part=False),
        entities,
    )


def test_warehouse_reconcile_requested_and_tick_guard_paths(monkeypatch):
    entities = SimpleNamespace(order_id="O1")
    requested = SimpleNamespace(state="requested")
    allocated = SimpleNamespace(state="allocated")

    same = warehouse_definition._reconcile_requested(
        object(),
        object(),
        entities=entities,
        order=allocated,
    )
    assert same is allocated

    monkeypatch.setattr(warehouse_definition, "allocate_order", lambda *args, **kwargs: False)
    blocked = warehouse_definition._reconcile_requested(
        object(),
        object(),
        entities=entities,
        order=requested,
    )
    assert blocked is None

    persistence = _EntityPersistence({("warehouse_fulfillment_order", "O1"): requested})
    monkeypatch.setattr(
        warehouse_definition,
        "_reconcile_requested",
        lambda *args, **kwargs: None,
    )
    warehouse_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        _default_config(warehouse_definition, auto_progress_fulfillment=True),
        entities,
    )

    warehouse_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        _default_config(warehouse_definition, auto_progress_fulfillment=False),
        entities,
    )


def test_warehouse_pick_pack_ship_progression(monkeypatch):
    entities = SimpleNamespace(order_id="O1")
    calls: list[str] = []
    progression = iter(
        [
            SimpleNamespace(state="picking"),
            SimpleNamespace(state="packed"),
        ]
    )

    class _ReloadingPersistence:
        def entity(self, kind: str, entity_id: str):
            assert kind == "warehouse_fulfillment_order"
            return next(progression)

    monkeypatch.setattr(
        warehouse_definition,
        "pick_order",
        lambda *args, **kwargs: calls.append("pick"),
    )
    monkeypatch.setattr(
        warehouse_definition,
        "pack_order",
        lambda *args, **kwargs: calls.append("pack"),
    )
    monkeypatch.setattr(
        warehouse_definition,
        "ship_order",
        lambda *args, **kwargs: calls.append("ship"),
    )

    warehouse_definition._reconcile_pick_pack_ship(
        _ReloadingPersistence(),
        object(),
        entities=entities,
        order=SimpleNamespace(state="allocated"),
    )

    assert calls == ["pick", "pack", "ship"]


def test_warehouse_pick_pack_ship_noop_path(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        warehouse_definition,
        "pick_order",
        lambda *args, **kwargs: calls.append("pick"),
    )
    monkeypatch.setattr(
        warehouse_definition,
        "pack_order",
        lambda *args, **kwargs: calls.append("pack"),
    )
    monkeypatch.setattr(
        warehouse_definition,
        "ship_order",
        lambda *args, **kwargs: calls.append("ship"),
    )

    warehouse_definition._reconcile_pick_pack_ship(
        _EntityPersistence({}),
        object(),
        entities=SimpleNamespace(order_id="O1"),
        order=SimpleNamespace(state="requested"),
    )

    assert calls == []


def test_order_to_cash_receivable_creation_and_receivable_state_edges(monkeypatch):
    entities = SimpleNamespace(order_id="SO1")
    persistence = _EntityPersistence({("receivable", order_to_cash_definition.receivable_id("SO1")): None})
    created = SimpleNamespace(id="R-created", state="open")
    monkeypatch.setattr(
        order_to_cash_definition,
        "ship_invoice_and_ensure_receivable",
        lambda *args, **kwargs: created,
    )
    receivable = order_to_cash_definition._receivable_or_create(
        persistence,
        object(),
        entities=entities,
    )
    assert receivable is created

    calls: list[str] = []
    monkeypatch.setattr(
        order_to_cash_definition,
        "schedule_overdue",
        lambda *args, **kwargs: calls.append("overdue"),
    )
    order_to_cash_definition._reconcile_receivable_state(
        object(),
        object(),
        object(),
        _default_config(order_to_cash_definition, auto_collect=False),
        entities=entities,
        receivable=SimpleNamespace(state="due"),
    )
    assert calls == ["overdue"]

    calls.clear()
    monkeypatch.setattr(
        order_to_cash_definition,
        "collect_receivable",
        lambda *args, **kwargs: calls.append("collect"),
    )
    order_to_cash_definition._reconcile_receivable_state(
        object(),
        object(),
        object(),
        _default_config(order_to_cash_definition, auto_collect=True),
        entities=entities,
        receivable=SimpleNamespace(state="overdue"),
    )
    assert calls == ["collect"]

    calls.clear()
    monkeypatch.setattr(
        order_to_cash_definition,
        "reconcile_collection",
        lambda *args, **kwargs: calls.append("collection"),
    )
    order_to_cash_definition._reconcile_receivable_state(
        object(),
        object(),
        object(),
        _default_config(order_to_cash_definition, auto_collect=False),
        entities=entities,
        receivable=SimpleNamespace(state="overdue"),
    )
    assert calls == ["collection"]


def test_order_to_cash_reconcile_tick_ignores_non_invoiced_terminal_states(monkeypatch):
    entities = SimpleNamespace(order_id="SO1")
    persistence = _EntityPersistence(
        {("sales_order", "SO1"): SimpleNamespace(state="cancelled")}
    )
    monkeypatch.setattr(
        order_to_cash_definition,
        "reconcile_credit",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_credit should not run")
        ),
    )
    monkeypatch.setattr(
        order_to_cash_definition,
        "reconcile_fulfillment",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_fulfillment should not run")
        ),
    )
    monkeypatch.setattr(
        order_to_cash_definition,
        "ship_invoice_and_ensure_receivable",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ship_invoice_and_ensure_receivable should not run")
        ),
    )

    order_to_cash_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        order_to_cash_definition.definition.default_config(),
        entities,
    )


def test_energy_definition_reconcile_tick_stops_when_demand_response_is_disabled(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        energy_definition,
        "record_meter_reading",
        lambda *args, **kwargs: calls.append("meter"),
    )
    monkeypatch.setattr(
        energy_definition,
        "schedule_demand_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("schedule_demand_response should not run when disabled")
        ),
    )
    monkeypatch.setattr(
        energy_definition,
        "reconcile_demand_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_demand_response should not run when disabled")
        ),
    )
    config = _default_config(
        energy_definition,
        auto_meter_reading=True,
        demand_response_enabled=False,
    )
    energy_definition._reconcile_tick(
        object(),
        object(),
        SimpleNamespace(now="N/A"),
        config,
        SimpleNamespace(),
    )
    assert calls == ["meter"]


def test_manufacturing_definition_reconcile_helpers_cover_waiting_and_quality_hold(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        manufacturing_definition,
        "reconcile_material_availability",
        lambda *args, **kwargs: calls.append("material"),
    )
    monkeypatch.setattr(
        manufacturing_definition,
        "_reconcile_released_order",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("released-order path should not run for waiting_material")
        ),
    )
    config = _default_config(
        manufacturing_definition,
        auto_seed_material=False,
    )
    manufacturing_definition._reconcile_tick(
        _EntityPersistence(
            {
                ("production_order", "PO1"): SimpleNamespace(
                    state="waiting_material",
                    attributes={},
                )
            }
        ),
        object(),
        object(),
        config,
        SimpleNamespace(production_order_id="PO1"),
    )
    assert calls == ["material"]

    calls.clear()
    monkeypatch.setattr(
        manufacturing_definition,
        "reconcile_quality_hold",
        lambda *args, **kwargs: calls.append("hold"),
    )
    manufacturing_definition._reconcile_inspection_state(
        object(),
        object(),
        object(),
        _default_config(manufacturing_definition, quality_outcome="hold"),
        entities=SimpleNamespace(),
    )
    assert calls == ["hold"]


def test_manufacturing_released_order_skips_setup_when_refresh_not_released(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        manufacturing_definition,
        "reconcile_material_availability",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        manufacturing_definition,
        "_refresh_order",
        lambda persistence, entities: None,
    )
    monkeypatch.setattr(
        manufacturing_definition,
        "reconcile_setup_resources",
        lambda *args, **kwargs: calls.append("setup"),
    )

    handled = manufacturing_definition._reconcile_released_order(
        object(),
        object(),
        object(),
        entities=SimpleNamespace(),
        order=SimpleNamespace(state="released"),
    )

    assert handled is True
    assert calls == []


def test_manufacturing_demand_surge_scenario_is_constructible():
    scenario = manufacturing_scenarios.demand_surge_scenario()
    assert scenario.name == "manufacturing-demand-surge"

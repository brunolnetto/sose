from __future__ import annotations

from types import SimpleNamespace

import pytest

from sose.examples.airports import definition as airports_definition
from sose.examples.energy_utilities import definition as energy_definition
from sose.examples.field_service import definition as field_service_definition
from sose.examples.hospitality import definition as hospitality_definition
from sose.examples.insurance import definition as insurance_definition
from sose.examples.itsm import definition as itsm_definition
from sose.examples.manufacturing import definition as manufacturing_definition
from sose.examples.manufacturing import scenarios as manufacturing_scenarios
from sose.examples.order_to_cash import definition as order_to_cash_definition
from sose.examples.p2p import definition as p2p_definition
from sose.examples.subscription_saas import definition as subscription_definition
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


def test_airports_reconcile_tick_skips_departure_when_turnaround_not_ready(monkeypatch):
    entities = SimpleNamespace(turnaround_id="T1")
    taxiing = SimpleNamespace(state="taxiing")
    persistence = _EntityPersistence({("airport_flight_turnaround", "T1"): taxiing})
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_gate_stage",
        lambda *args, **kwargs: (taxiing, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_ground_stage",
        lambda *args, **kwargs: (taxiing, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "_reconcile_baggage_stage",
        lambda *args, **kwargs: (taxiing, True),
    )
    monkeypatch.setattr(
        airports_definition,
        "reconcile_departure",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_departure should not run for non-ready turnaround")
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


def test_warehouse_reconcile_allocates_then_delegates_to_service_flow(monkeypatch):
    entities = SimpleNamespace(order_id="O1")
    allocated = SimpleNamespace(state="allocated")
    persistence = _EntityPersistence(
        {("warehouse_fulfillment_order", "O1"): allocated}
    )
    calls: list[str] = []

    monkeypatch.setattr(
        warehouse_definition,
        "_reconcile_requested",
        lambda *args, **kwargs: allocated,
    )
    monkeypatch.setattr(
        warehouse_definition,
        "reconcile_fulfillment_services",
        lambda *args, **kwargs: calls.append("services"),
    )

    warehouse_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        warehouse_definition.definition.default_config(),
        entities,
    )

    assert calls == ["services"]


def test_warehouse_reconcile_stops_when_allocation_cannot_progress(monkeypatch):
    entities = SimpleNamespace(order_id="O1")
    requested = SimpleNamespace(state="requested")
    persistence = _EntityPersistence(
        {("warehouse_fulfillment_order", "O1"): requested}
    )

    monkeypatch.setattr(
        warehouse_definition,
        "_reconcile_requested",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        warehouse_definition,
        "reconcile_fulfillment_services",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("service flow must not run without an allocation")
        ),
    )

    warehouse_definition._reconcile_tick(
        persistence,
        object(),
        object(),
        warehouse_definition.definition.default_config(),
        entities,
    )


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


def test_manufacturing_definition_ignores_unknown_terminal_state(monkeypatch):
    monkeypatch.setattr(
        manufacturing_definition,
        "_reconcile_released_order",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        manufacturing_definition,
        "release_setup_resources",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("release_setup_resources should not run for unknown state")
        ),
    )

    manufacturing_definition._reconcile_tick(
        _EntityPersistence(
            {
                ("production_order", "PO1"): SimpleNamespace(
                    state="archived",
                    attributes={},
                )
            }
        ),
        object(),
        object(),
        _default_config(manufacturing_definition, auto_seed_material=False),
        SimpleNamespace(production_order_id="PO1"),
    )


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


def test_hospitality_definition_reservation_auto_progress_guards(monkeypatch):
    entities = SimpleNamespace()
    config = _default_config(
        hospitality_definition,
        auto_progress_reservation=False,
    )
    monkeypatch.setattr(
        hospitality_definition,
        "create_hold",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("create_hold should not run when auto progression is disabled")
        ),
    )
    hospitality_definition._reconcile_tick(
        object(),
        object(),
        SimpleNamespace(now="N/A"),
        config,
        entities,
    )

    arrival_calls: list[object] = []
    monkeypatch.setattr(
        hospitality_definition,
        "create_hold",
        lambda *args, **kwargs: arrival_calls.append(kwargs["arrival_at"]) or SimpleNamespace(id="R1"),
    )
    monkeypatch.setattr(
        hospitality_definition,
        "confirm_reservation",
        lambda *args, **kwargs: None,
    )
    config = _default_config(
        hospitality_definition,
        auto_progress_reservation=True,
    )
    backend = SimpleNamespace(now=config.start_at + config.arrival_after, tick_step=config.tick_step)
    hospitality_definition._reconcile_tick(
        _EntityPersistence({("hospitality_reservation", hospitality_definition.reservation_id(1)): None}),
        object(),
        backend,
        config,
        entities,
    )
    assert arrival_calls == [backend.now + config.tick_step]


def test_subscription_definition_effective_time_and_inactive_guard():
    config = subscription_definition.definition.default_config()
    assert subscription_definition._effective_plan_change_at(
        config=config,
        backend=SimpleNamespace(now=config.start_at + config.plan_change_after),
    ) == config.start_at + config.plan_change_after + config.tick_step

    subscription_definition._reconcile_tick(
        _EntityPersistence(
            {
                (
                    "saas_subscription",
                    "S1",
                ): SimpleNamespace(
                    state="ended",
                    attributes={"plan_code": "starter", "change_request_ids": []},
                )
            }
        ),
        object(),
        SimpleNamespace(now=config.start_at),
        config,
        SimpleNamespace(subscription_id="S1"),
    )


def test_p2p_definition_missing_reference_and_no_auto_consume_branch(monkeypatch):
    with pytest.raises(RuntimeError, match="reference entities were not persisted"):
        p2p_definition._reconcile_tick(
            _EntityPersistence({}),
            object(),
            object(),
            p2p_definition.definition.default_config(),
            SimpleNamespace(
                purchase_order_id="PO1",
                receipt_id="R1",
                material_demand_id="D1",
            ),
        )

    monkeypatch.setattr(
        p2p_definition,
        "reconcile_consumption",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_consumption should not run when auto consume is disabled")
        ),
    )
    p2p_definition._reconcile_tick(
        _EntityPersistence(
            {
                ("purchase_order", "PO1"): SimpleNamespace(state="received"),
                ("receipt", "R1"): SimpleNamespace(state="stocked"),
                ("material_demand", "D1"): SimpleNamespace(state="pending"),
            }
        ),
        object(),
        object(),
        _default_config(p2p_definition, auto_consume_inventory=False),
        SimpleNamespace(
            purchase_order_id="PO1",
            receipt_id="R1",
            material_demand_id="D1",
            quantity=10.0,
        ),
    )


def test_itsm_and_insurance_definition_guard_edges(monkeypatch):
    escalation_calls: list[str] = []
    monkeypatch.setattr(
        itsm_definition,
        "reconcile_escalation",
        lambda *args, **kwargs: escalation_calls.append("escalate"),
    )
    monkeypatch.setattr(
        itsm_definition,
        "_reload_incident",
        lambda persistence, incident_id: SimpleNamespace(id=incident_id, state="in_progress"),
    )
    escalated = itsm_definition._reconcile_escalated_incident(
        object(),
        object(),
        object(),
        incident=SimpleNamespace(id="I1", state="escalated"),
        claim_id="claim-1",
    )
    assert escalated is not None and escalated.state == "in_progress"
    assert escalation_calls == ["escalate"]
    assert itsm_definition._reconcile_resolution(
        object(),
        object(),
        object(),
        incident=None,
        claim_id="claim-1",
    ) is None

    insurance_calls: list[str] = []
    monkeypatch.setattr(
        insurance_definition,
        "satisfy_documents",
        lambda *args, **kwargs: insurance_calls.append("docs"),
    )
    handled = insurance_definition._maybe_satisfy_documents(
        _EntityPersistence({("insurance_document_request", insurance_definition.document_request_id("C1", 1)): None}),
        object(),
        _default_config(insurance_definition, auto_satisfy_documents=True),
        entities=SimpleNamespace(),
        claim=SimpleNamespace(id="C1", state="pending_documents"),
    )
    assert handled is True
    assert insurance_calls == []

    monkeypatch.setattr(
        insurance_definition,
        "complete_assessment",
        lambda *args, **kwargs: insurance_calls.append("assess"),
    )
    assert insurance_definition._maybe_assess_claim(
        _EntityPersistence(
            {
                ("insurance_assessment", insurance_definition.assessment_id("C1", 1)): SimpleNamespace(state="queued")
            }
        ),
        object(),
        object(),
        _default_config(insurance_definition),
        entities=SimpleNamespace(),
        claim=SimpleNamespace(id="C1", state="assessing"),
    ) is True
    assert insurance_calls == []

    monkeypatch.setattr(
        insurance_definition,
        "reconcile_payment",
        lambda *args, **kwargs: insurance_calls.append("pay"),
    )
    insurance_definition._maybe_reconcile_payment(
        _EntityPersistence(
            {
                ("insurance_payment", insurance_definition.payment_id("C1")): SimpleNamespace(state="scheduled")
            }
        ),
        object(),
        object(),
        _default_config(insurance_definition),
        entities=SimpleNamespace(),
        claim=SimpleNamespace(state="payment_scheduled", id="C1"),
    )
    assert insurance_calls == []

    fraud_calls: list[str] = []
    monkeypatch.setattr(
        insurance_definition,
        "reconcile_fraud",
        lambda *args, **kwargs: fraud_calls.append("fraud"),
    )
    insurance_definition._reconcile_tick(
        _EntityPersistence({("insurance_claim", "C1"): SimpleNamespace(id="C1", state="fraud_review")}),
        object(),
        object(),
        _default_config(insurance_definition),
        SimpleNamespace(claim_id="C1"),
    )
    assert fraud_calls == ["fraud"]

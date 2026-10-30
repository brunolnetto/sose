"""#590: fail-closed recovery and warehouse-owned inventory invariants.

These are bounded reference-adapter tests: a constructor-only fenced-store
fixture is not a substitute for real PostgreSQL multi-worker conformance.
Warehouse allocations and occurrences use genuine MemoryPersistence records.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.domain.entity import Entity
from sose.examples.warehouse_fulfillment import simulation as warehouse
from sose.examples.warehouse_fulfillment.entities import (
    FulfillmentServiceTask, InventoryOccurrence,
)
from sose.persistence.memory import MemoryPersistence


OWNER = "worker:recovery-coverage"
ORG = "organization:operational-coverage"


class _FencedWriter:
    """Constructor conformance: all writer calls would be observable."""
    dsn = "postgresql://local/authority"

    def __init__(self):
        self.calls = []

    def writer_epoch(self):
        self.calls.append("writer_epoch")
        return 0

    def claim_writer(self, *args, **kwargs):
        self.calls.append("claim_writer")
        raise AssertionError("constructor must not acquire a writer lease")


def _runner(**overrides):
    store = _FencedWriter()
    config = {"persistence": store, "owner_id": OWNER}
    config.update(overrides)
    return TradingCustomerRecoveryRunner(**config), store


@pytest.mark.parametrize(
    "options, error, type_",
    [
        ({"owner_id": ""}, "worker owner and job identity", ValueError),
        ({"job_id": ""}, "worker owner and job identity", ValueError),
        ({"max_actions": 0}, "max_actions must be", ValueError),
        ({"lease_duration": timedelta(0)}, "lease_duration must be", ValueError),
        ({"lease_duration": timedelta(seconds=-1)}, "lease_duration must be", ValueError),
        ({"correlation_id": ""}, "correlation_id cannot be empty", ValueError),
        ({"scoped_writer": True}, "one explicit correlation_id", ValueError),
        ({"scoped_writer": True, "correlation_id": "flow:a"},
         "PostgreSQL scoped fencing", TypeError),
        ({"resource_persistence": object()},
         "shared resource persistence requires explicit policies", ValueError),
        ({"resource_persistence": object(), "resource_policies": {}},
         "domain and physical resource ledgers must share PostgreSQL", ValueError),
        ({"organization_owners": {"flow:a": ""}},
         "organization_owners must contain valid durable bindings", ValueError),
        ({"organization_owners": {"": ORG}},
         "organization_owners must contain valid durable bindings", ValueError),
        ({"resource_policies": {}, "resource_retry_delay": timedelta(0),
          "resource_organizations": {"flow:a": ORG}},
         "resource slot and retry delay must be positive", ValueError),
        ({"resource_policies": {}, "resource_slot_duration": timedelta(0),
          "resource_organizations": {"flow:a": ORG}},
         "resource slot and retry delay must be positive", ValueError),
        ({"resource_policies": {}, "resource_organizations": {}},
         "require explicit correlation-to-organization", ValueError),
        ({"resource_policies": {}, "resource_organizations": {"flow:a": ""}},
         "require explicit correlation-to-organization", ValueError),
        ({"resource_policies": {}, "resource_organizations": {"flow:a": ORG},
          "correlation_id": "flow:other"},
         "resource mapping lacks the owned correlation_id", ValueError),
    ],
)
def test_recovery_configuration_refuses_ambiguous_fencing_and_physical_authority(
    options, error, type_,
):
    store = _FencedWriter()
    with pytest.raises(type_, match=error):
        TradingCustomerRecoveryRunner(
            persistence=store, owner_id=OWNER, **options
        ) if "owner_id" not in options else TradingCustomerRecoveryRunner(
            persistence=store, **{"owner_id": OWNER, **options}
        )
    assert store.calls == []


def test_recovery_scoped_owner_mapping_requires_stable_explicit_binding():
    runner, store = _runner(
        correlation_id="flow:a",
        organization_owners={"flow:a": ORG, "flow:b": "other"},
        resource_organizations={"flow:a": ORG},
    )
    assert runner._egress_organization_owners() == {"flow:a": ORG}
    assert runner._organization_for("flow:a") == ORG
    with pytest.raises(ValueError, match="explicit correlation-to-organization"):
        runner._organization_for("flow:missing")
    assert store.calls == []


def test_recovery_egress_rejects_disagreement_between_physical_and_operational_owner():
    runner, store = _runner(
        organization_owners={"flow:a": ORG},
        resource_organizations={"flow:a": "organization:forged"},
    )
    with pytest.raises(ValueError, match="bindings disagree"):
        runner._organization_for("flow:a")
    with pytest.raises(ValueError, match="bindings disagree"):
        runner._egress_organization_owners()
    assert store.calls == []


def test_recovery_egress_can_resolve_only_explicit_physical_binding():
    runner, store = _runner(
        resource_organizations={"flow:a": ORG},
    )
    assert runner._egress_organization_owners() == {"flow:a": ORG}
    assert store.calls == []


@pytest.mark.parametrize(
    "invalid, error",
    [
        ({"requested_quantity": 0}, "requested_quantity must be positive"),
        ({"requested_quantity": -2}, "requested_quantity must be positive"),
        ({"requested_sku": ""}, "requested_sku cannot be empty"),
        ({"request_key": ""}, "composed request_key must be nonempty"),
        ({"picker_capacity": 0}, "picker_capacity must be at least one"),
        ({"packing_station_capacity": 0}, "packing_station_capacity must be at least one"),
        ({"shipping_dock_capacity": 0}, "shipping_dock_capacity must be at least one"),
    ],
)
def test_composed_fulfillment_refuses_invalid_inputs_before_durable_creation(
    invalid, error,
):
    store = MemoryPersistence()
    with pytest.raises(ValueError, match=error):
        warehouse.seed_composed_reference(
            store, owner_id=ORG, **invalid
        )
    assert store.entities() == ()
    assert store.resource_definitions() == ()


def _composed():
    store = MemoryPersistence()
    entities = warehouse.seed_composed_reference(
        store, owner_id=ORG, request_key="order:coverage",
    )
    _, engine = warehouse.build_runtime(store, owner_id=ORG)
    return store, entities, engine


def _allocate(store, entities, engine, **changes):
    data = dict(
        stock_reference="wm:stock:original",
        reservation_reference="wm:reservation:original",
        supplied_sku=warehouse.PRIMARY_SKU,
        quantity=10.0,
    )
    data.update(changes)
    return warehouse.allocate_composed_order(
        store, engine, entities=entities, **data
    )


@pytest.mark.parametrize(
    "change, error",
    [
        ({"stock_reference": ""}, "stock_reference cannot be empty"),
        ({"reservation_reference": ""}, "reservation_reference cannot be empty"),
        ({"supplied_sku": ""}, "supplied_sku cannot be empty"),
        ({"quantity": 0}, "quantity must be positive"),
        ({"quantity": -1}, "quantity must be positive"),
        ({"quantity": 9}, "quantity must satisfy the order"),
        ({"supplied_sku": "unacceptable-sku"}, "SKU is not acceptable"),
    ],
)
def test_composed_allocation_refuses_foreign_or_insufficient_inventory_evidence(
    change, error,
):
    store, entities, engine = _composed()
    with pytest.raises(ValueError, match=error):
        _allocate(store, entities, engine, **change)
    order = store.entity("warehouse_fulfillment_order", entities.order_id)
    assert order.state == "requested"
    assert order.attributes["allocation_ids"] == []
    assert not any(
        entity.entity_type == "warehouse_allocation" for entity in store.entities()
    )


def test_composed_allocation_refuses_local_inventory_ownership_mutation():
    store, entities, engine = _composed()
    with store.transaction() as uow:
        order = uow.get_entity("warehouse_fulfillment_order", entities.order_id)
        order.attributes["inventory_owner"] = "warehouse_fulfillment"
        uow.save_entity(order)
    with pytest.raises(ValueError, match="requires WM-owned inventory"):
        _allocate(store, entities, engine)
    assert not any(
        entity.entity_type == "warehouse_allocation" for entity in store.entities()
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"stock_reference": "wm:stock:foreign"},
        {"reservation_reference": "wm:reservation:foreign"},
        {"quantity": 10.0000000001},
    ],
)
def test_composed_allocation_replay_cannot_rebind_original_warehouse_certificate(changed):
    store, entities, engine = _composed()
    assert _allocate(store, entities, engine)
    saved = store.entity("warehouse_fulfillment_order", entities.order_id)
    assert saved.state == "allocated"
    original_ids = tuple(saved.attributes["allocation_ids"])
    assert len(original_ids) == 1
    with pytest.raises(ValueError, match="composed allocation replay conflict"):
        _allocate(store, entities, engine, **changed)
    after = store.entity("warehouse_fulfillment_order", entities.order_id)
    assert tuple(after.attributes["allocation_ids"]) == original_ids


def test_composed_allocation_rejects_ambiguous_multiple_durable_certificates():
    store, entities, engine = _composed()
    assert _allocate(store, entities, engine)
    with store.transaction() as uow:
        order = uow.get_entity("warehouse_fulfillment_order", entities.order_id)
        order.attributes["allocation_ids"].append("allocation:forged")
        uow.save_entity(order)
    with pytest.raises(RuntimeError, match="must have one reservation allocation"):
        _allocate(store, entities, engine)


def test_composed_allocation_is_idempotent_on_exact_replay():
    store, entities, engine = _composed()
    assert _allocate(store, entities, engine) is True
    before = store.entity("warehouse_fulfillment_order", entities.order_id)
    ids = tuple(before.attributes["allocation_ids"])
    assert _allocate(store, entities, engine) is True
    after = store.entity("warehouse_fulfillment_order", entities.order_id)
    assert tuple(after.attributes["allocation_ids"]) == ids


def test_inventory_occurrence_rejects_changed_source_evidence_before_commit():
    occurrence = InventoryOccurrence(
        id="occurrence:immutable", state="captured",
        attributes={"order_id": "order:canonical", "quantity": 10},
    )
    warehouse._validate_occurrence_identity(
        occurrence, {"order_id": "order:canonical", "quantity": 10}
    )
    with pytest.raises(ValueError, match="different evidence"):
        warehouse._validate_occurrence_identity(
            occurrence, {"order_id": "order:foreign"}
        )


def test_occurrence_owner_reference_is_idempotent_and_never_creates_on_none():
    order = Entity(
        id="order:canonical", entity_type="warehouse_fulfillment_order",
        state="requested", attributes={"occurrence_ids": ["occ:old"]},
    )
    warehouse._append_occurrence_id(None, "occ:new")
    warehouse._append_occurrence_id(order, "occ:old")
    assert order.attributes["occurrence_ids"] == ["occ:old"]
    warehouse._append_occurrence_id(order, "occ:new")
    warehouse._append_occurrence_id(order, "occ:new")
    assert order.attributes["occurrence_ids"] == ["occ:old", "occ:new"]


@pytest.mark.parametrize(
    "stage, expected_error",
    [
        ("pick", "resource identity changed"),
        ("pack", "duration changed after creation"),
    ],
)
def test_durable_service_task_cannot_change_resource_identity_or_duration(
    stage, expected_error,
):
    store, entities, engine = _composed()
    subject_id = "subject:1"
    task_id = warehouse.service_task_id(stage, subject_id)
    prior = FulfillmentServiceTask(
        id=task_id, state="queued",
        attributes={
            "resource_name": "original:picker",
            "service_duration_seconds": 120.0,
            "stage": stage, "subject_id": subject_id,
            "order_id": entities.order_id,
        },
    )
    with store.transaction() as uow:
        uow.save_entity(prior)
    kwargs = dict(
        stage=stage, subject_id=subject_id,
        order_id=entities.order_id, resource_name="original:picker",
        service_duration=timedelta(seconds=120),
    )
    saved = warehouse._ensure_service_task(store, engine, **kwargs)
    assert saved.id == task_id
    if stage == "pick":
        kwargs["resource_name"] = "foreign:picker"
    else:
        kwargs["service_duration"] = timedelta(seconds=150)
    with pytest.raises(ValueError, match=expected_error):
        warehouse._ensure_service_task(store, engine, **kwargs)


def test_service_task_refuses_zero_duration_before_mutating_domain_state():
    store, entities, engine = _composed()
    with pytest.raises(ValueError, match="service duration must be positive"):
        warehouse._ensure_service_task(
            store, engine, stage="pick", subject_id="subject:negative",
            order_id=entities.order_id,
            resource_name=warehouse.PICKER_RESOURCE,
            service_duration=timedelta(0),
        )
    assert not any(
        entity.entity_type == "warehouse_fulfillment_service_task"
        for entity in store.entities()
    )


def test_service_reconciliation_rejects_unallocated_order_before_any_physical_work():
    store, entities, engine = _composed()
    with pytest.raises(RuntimeError, match="requires allocated fulfillment order"):
        warehouse.reconcile_fulfillment_services(
            store, engine, None, entities=entities,
            pick_duration=timedelta(seconds=10),
            pack_duration=timedelta(seconds=10),
            ship_duration=timedelta(seconds=10),
        )
    assert not any(
        entity.entity_type == "warehouse_fulfillment_service_task"
        for entity in store.entities()
    )

"""#496: canonical, owner-qualified organization snapshot.

Memory, SQLite incremental and (with ``SOSE_TEST_POSTGRES_DSN``) PostgreSQL.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import os

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.organization import OrganizationDefinition, ProcessComponent
from sose.composition.organization_snapshot import snapshot_organization
from sose.core.events import DomainEvent
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from tests.integration.sose.composition import test_organization_boundary_backends as p496a
from tests.integration.sose.composition.test_attested_component_jobs import (  # noqa: F401
    FULFILL, T0, WAREHOUSE, build, definition, opener,
)

# ``opener`` and ``store`` are imported fixtures requested by name.
# ruff: noqa: F811
store = p496a.store
DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")


def snapshot(persistence, spec=None):
    return snapshot_organization(
        spec or definition(), persistence, catalog=builtin_catalog(),
        manifests=builtin_process_manifests(), consumers=BoundaryConsumerRegistry(),
    )


def workload(persistence):
    jobs = build(persistence)
    for job in jobs.values():
        job.runner.initialize({})
        for index in range(3):
            job.runner.run_trigger(trigger_id=f"{job.scheduled.job_id}-{index}", triggered_at=T0)


def test_snapshot_projects_each_component_and_writes_nothing(opener):
    persistence = opener()
    workload(persistence)
    before = (persistence.job_states(), persistence.events(), persistence.entities())
    first = snapshot(persistence)
    assert (persistence.job_states(), persistence.events(), persistence.entities()) == before

    fulfillment, warehouse = first.components
    assert (fulfillment.component_id, warehouse.component_id) == ("fulfillment", "warehouse")
    assert [(m.entity_type, m.state) for m in fulfillment.machines] == [
        ("warehouse_fulfillment_order", "picking"),
    ]
    assert fulfillment.machines[0].last_event_id == fulfillment.transition_event_ids[-1]
    assert len(fulfillment.pending_commands) == len(fulfillment.scheduled_work_ids) == 1
    assert [(job.job_id, job.attested) for job in fulfillment.jobs] == [(FULFILL, True)]
    assert [(job.job_id, job.attested) for job in warehouse.jobs] == [(WAREHOUSE, True)]
    # Seeded warehouse sites never transitioned: no owner evidence, listed once.
    assert warehouse.machines == ()
    # Another owner's scheduled work and commands are never attributed here.
    assert warehouse.scheduled_work_ids == () and warehouse.pending_commands == ()
    assert {row[0] for row in first.unattributed_machines} == {"warehouse_management_site"}
    assert first.boundary == ()
    assert snapshot(persistence) == first


def test_the_digest_survives_a_restart_and_tracks_progress(opener):
    first = opener()
    workload(first)
    digest = snapshot(first).digest
    reopened = opener()
    assert snapshot(reopened).digest == digest
    build(reopened)[FULFILL].runner.run_trigger(trigger_id="more", triggered_at=T0)
    assert snapshot(reopened).digest != digest


@pytest.mark.parametrize("other", [
    "sqlite-incremental",
    pytest.param("postgres", marks=pytest.mark.skipif(not DSN, reason="PostgreSQL")),
])
def test_the_same_durable_state_has_the_same_digest_on_every_adapter(tmp_path, other):
    memory = MemoryPersistence()
    workload(memory)
    if other == "sqlite-incremental":
        persistence = SQLiteIncrementalPersistence(tmp_path / "equal.sqlite3")
    else:
        from uuid import uuid4

        from sose.persistence.postgres import PostgresPersistence
        persistence = PostgresPersistence(DSN, namespace=f"snap_{uuid4().hex[:12]}")
    try:
        workload(persistence)
        assert snapshot(persistence) == snapshot(memory)
    finally:
        persistence.close()


def test_a_stored_state_that_contradicts_the_last_transition_fails_closed(opener):
    persistence = opener()
    workload(persistence)
    [machine] = snapshot(persistence).components[0].machines
    with persistence.transaction() as uow:
        entity = uow.get_entity(machine.entity_type, machine.entity_id)
        uow.save_entity(replace(entity, state="tampered"))
    with pytest.raises(ValueError, match="contradicts its owner's last transition"):
        snapshot(persistence)


def test_a_machine_transitioned_by_two_owners_fails_closed():
    """Two components of one domain may share entity types; one machine, one owner."""
    persistence = MemoryPersistence()
    spec = OrganizationDefinition(
        organization_id="org:two-fulfillers", version=1,
        components=(
            ProcessComponent("east", "warehouse_fulfillment", "owner:east",
                             ("FulfillmentOrderChart",)),
            ProcessComponent("west", "warehouse_fulfillment", "owner:west",
                             ("FulfillmentOrderChart",)),
        ),
    )
    with persistence.transaction() as uow:
        for index, owner in enumerate(("owner:east", "owner:west")):
            uow.append_event(DomainEvent(
                event_id=f"event-{index}", name="entity.state_transition",
                entity_type="warehouse_fulfillment_order", entity_id="order-1",
                occurred_at=T0, owner_id=owner,
                payload={"trigger": "t", "from_state": "a", "to_state": "b"},
            ))
    with pytest.raises(ValueError, match="transitioned by two owners"):
        snapshot(persistence, spec)


def test_scheduled_work_naming_a_missing_command_fails_closed(monkeypatch):
    """Persistence refuses that state on write; the snapshot still fails closed on it."""
    persistence = MemoryPersistence()
    workload(persistence)
    monkeypatch.setattr(MemoryPersistence, "command", lambda self, command_id: None)
    with pytest.raises(ValueError, match="names a missing command"):
        snapshot(persistence)


def test_a_store_without_read_snapshot_is_refused():
    class NoSnapshot:
        pass

    with pytest.raises(TypeError, match="read_snapshot"):
        snapshot(NoSnapshot())


def test_owner_qualified_boundary_messages_are_projected(store):
    definition_ = p496a._definition()
    service = p496a.OrganizationBoundaryService(store, p496a._router(definition_))
    p496a._seed_source_event(store)
    request = p496a._request(definition_)
    service.publish(request)
    pending = snapshot_organization(
        definition_, store, catalog=p496a._catalog(), manifests=p496a._manifests(),
        consumers=p496a._domain_contracts(),
    )
    [record] = pending.boundary
    assert (record.message_id, record.consumed) == (request.message_id, False)
    assert record.definition_fingerprint == definition_.fingerprint()
    assert (record.source_component_id, record.destination_component_id) == (
        request.source_component_id, request.destination_component_id,
    )
    p496a._claim_and_consume(service, request, p496a.NOW + timedelta(seconds=6))
    consumed = snapshot_organization(
        definition_, store, catalog=p496a._catalog(), manifests=p496a._manifests(),
        consumers=p496a._domain_contracts(),
    )
    assert consumed.boundary[0].consumed and consumed.digest != pending.digest
    # Another organization's snapshot of the same namespace sees none of these.
    other = replace(definition_, organization_id="org:other-organization")
    assert snapshot_organization(
        other, store, catalog=p496a._catalog(), manifests=p496a._manifests(),
        consumers=p496a._domain_contracts(),
    ).boundary == ()

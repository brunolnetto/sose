"""Deterministic live-PostgreSQL Logistics transfer contention falsifiers (#489).

An independent session holds the exact finite vehicle, destination dock, or
destination queue while the shipment's own real StateChart attempts transfer.
Neither a random multiworker rerun nor a mocked resource failure qualifies.
"""
from __future__ import annotations

import os
from threading import Event, Thread
from uuid import uuid4

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.logistics import simulation as logistics
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
SHIPMENT_OWNER = "pc6-transfer-shipment-organization"
HOLDER_OWNER = "pc6-transfer-holder-organization"
UNBLOCKED_OWNER = "pc6-transfer-unblocked-organization"


def _rebuild(store, *, owner_id):
    """Recreate ephemeral SimPy exactly at this owner's durable clock."""
    position = store.simulation_position(owner_id=owner_id)
    origin = logistics.ORIGIN if position is None else position.logical_time
    _, engine = logistics.build_runtime(store, owner_id=owner_id, now=origin)
    backend = SimPyBackend(origin=origin)
    engine.rebuild_backend(backend)
    return engine, backend


def _snapshot(store):
    """Complete comparable evidence for repeated transfer reconciliation."""
    return (
        tuple(store.events()),
        tuple(store.resource_demands()),
        tuple(store.resource_reservations()),
        tuple(store.resource_release_intents()),
        tuple(store.store_items()),
        tuple(store.store_put_intents()),
        tuple(store.store_get_requests()),
        tuple(store.store_get_results()),
    )


@pytest.mark.parametrize(
    ("blocker", "blocked_resource"),
    [
        ("transfer_vehicle", "transfer_vehicle"),
        ("destination_dock", "destination_dock"),
        ("destination_put", None),
    ],
)
def test_pg_transfer_finite_contention_retries_across_independent_sessions(
    blocker, blocked_resource,
):
    assert DSN is not None
    namespace = "pc6_transfer_finite_" + uuid4().hex[:12]
    held, release, released = Event(), Event(), Event()
    holder_errors: list[BaseException] = []
    blocker_id = f"destination-queue:foreign-filler:{blocker}"
    holder_shipment = None

    with PostgresPersistence(DSN, namespace=namespace) as store:
        # Physical capacity is immutable once seeded. Declare a second
        # vehicle before any domain StateChart begins; the destination dock
        # remains single-capacity and can independently become contended.
        overrides = (
            {"transfer_vehicle": 2} if blocker == "destination_dock" else None
        )
        shipment = logistics.seed_reference(
            store, owner_id=SHIPMENT_OWNER, instance_key="customer:"+namespace,
            resource_capacity=1, resource_capacities=overrides,
            hub_queue_capacity=1,
        )
        holder_shipment = logistics.seed_reference(
            store, owner_id=HOLDER_OWNER, instance_key="holder:"+namespace,
            resource_capacity=1, resource_capacities=overrides,
            hub_queue_capacity=1,
        )
        unblocked_shipment = logistics.seed_reference(
            store, owner_id=UNBLOCKED_OWNER, instance_key="unblocked:"+namespace,
            resource_capacity=1, resource_capacities=overrides,
            hub_queue_capacity=1,
        )
        engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
        backend.run_until(logistics.PICKUP_DUE)
        assert logistics.reconcile_pickup(store, engine, backend, entities=shipment)
        assert logistics.reconcile_origin_hub(store, engine, backend, entities=shipment)
        assert store.entity("shipment", shipment.shipment_id).state == "at_origin_hub"

    def foreign_real_statechart():
        try:
            with PostgresPersistence(DSN, namespace=namespace) as store:
                engine, backend = _rebuild(store, owner_id=HOLDER_OWNER)
                backend.run_until(logistics.PICKUP_DUE)
                if blocked_resource is not None:
                    # A *second* independently owned Logistics StateChart
                    # reaches the origin, then owns physical grants while
                    # its destination queue is blocked.
                    assert logistics.reconcile_pickup(
                        store, engine, backend, entities=holder_shipment,
                    )
                    assert logistics.reconcile_origin_hub(
                        store, engine, backend, entities=holder_shipment,
                    )
                    assert store.entity(
                        "shipment", holder_shipment.shipment_id
                    ).state == "at_origin_hub"
                    queue = logistics._organization_queue(
                        HOLDER_OWNER, "destination_hub_queue",
                    )
                else:
                    # A second session controls a consumer of the shipment's
                    # finite destination queue. Its GET receipt must survive
                    # after the item itself is removed.
                    queue = logistics._organization_queue(
                        SHIPMENT_OWNER, "destination_hub_queue",
                    )

                engine.stores.ensure_put(
                    backend, store_name=queue, item_id=blocker_id,
                    value={"shipment_id": "finite-queue-filler"},
                    requested_at=backend.now,
                )
                backend.run_until(backend.now)
                assert any(
                    item.store_name == queue and item.item_id == blocker_id
                    for item in store.store_items()
                )
                if blocked_resource is not None:
                    assert logistics.reconcile_transfer(
                        store, engine, backend, entities=holder_shipment,
                    ) is False
                    assert store.entity(
                        "shipment", holder_shipment.shipment_id
                    ).state == "in_transfer"
                    assert engine.resources.reservation_for(
                        f"transfer-vehicle:{holder_shipment.shipment_id}"
                    ) is not None
                    assert engine.resources.reservation_for(
                        f"destination-dock:{holder_shipment.shipment_id}"
                    ) is not None

                held.set()
                if not release.wait(timeout=40):
                    raise TimeoutError("contender never reached blocked transfer")

                taken = engine.stores.ensure_selection(
                    backend, store_name=queue,
                    request_id=f"foreign-dequeue:{blocker}",
                    requested_at=backend.now,
                )
                assert taken is not None and taken.item.item_id == blocker_id
                backend.run_until(backend.now)

                if blocked_resource is not None:
                    # The holder StateChart itself releases the vehicle/dock
                    # via arrive_destination_hub; not a manual manager release.
                    assert logistics.reconcile_transfer(
                        store, engine, backend, entities=holder_shipment,
                    ) is True
                    assert store.entity(
                        "shipment", holder_shipment.shipment_id
                    ).state == "at_destination_hub"
                    assert engine.resources.has_request(
                        f"transfer-vehicle:{holder_shipment.shipment_id}"
                    ) is False
                    assert engine.resources.has_request(
                        f"destination-dock:{holder_shipment.shipment_id}"
                    ) is False
                else:
                    # A pending PUT was committed by the contender in a
                    # different SimPy Environment. Rebuild it here, fill the
                    # newly vacant slot, and GET the actual shipment before
                    # its original owner resumes after PostgreSQL reopen.
                    backend = SimPyBackend(origin=backend.now)
                    engine.rebuild_backend(backend)
                    backend.run_until(backend.now)
                    result = engine.stores.ensure_selection(
                        backend, store_name=queue,
                        request_id=f"foreign-receive:{shipment.shipment_id}",
                        requested_at=backend.now,
                    )
                    assert result is not None
                    assert result.item.item_id == (
                        f"destination-queue:{shipment.shipment_id}"
                    )
                    backend.run_until(backend.now)
                    assert any(
                        receipt.request_id == result.request_id
                        and receipt.item.item_id == result.item.item_id
                        for receipt in store.store_get_results()
                    )
                    assert not any(
                        item.item_id == result.item.item_id
                        for item in store.store_items()
                    )
        except BaseException as error:
            holder_errors.append(error)
            held.set()
        finally:
            released.set()

    foreign = Thread(target=foreign_real_statechart, daemon=True)
    foreign.start()
    try:
        assert held.wait(timeout=30), "independent worker never reached holding phase"
        assert not holder_errors, repr(holder_errors)
        with PostgresPersistence(DSN, namespace=namespace) as store:
            engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
            assert logistics.reconcile_transfer(
                store, engine, backend, entities=shipment,
            ) is False
            state = store.entity("shipment", shipment.shipment_id)
            assert state is not None
            assert state.state == (
                "at_origin_hub" if blocker == "transfer_vehicle" else "in_transfer"
            )
            vehicle_id = f"transfer-vehicle:{shipment.shipment_id}"
            dock_id = f"destination-dock:{shipment.shipment_id}"
            if blocker == "transfer_vehicle":
                assert engine.resources.has_request(vehicle_id)
                assert engine.resources.reservation_for(vehicle_id) is None
            elif blocker == "destination_dock":
                assert engine.resources.reservation_for(vehicle_id) is not None
                assert engine.resources.has_request(dock_id)
                assert engine.resources.reservation_for(dock_id) is None
            else:
                assert engine.resources.reservation_for(dock_id) is not None
                assert sum(
                    intent.item_id == f"destination-queue:{shipment.shipment_id}"
                    for intent in store.store_put_intents()
                ) == 1
            if blocked_resource is not None:
                assert [
                    r.request_id for r in store.resource_reservations()
                    if r.resource_name == blocked_resource
                ] == [
                    f"{'transfer-vehicle' if blocker == 'transfer_vehicle' else 'destination-dock'}:"
                    f"{holder_shipment.shipment_id}"
                ]
            events = store.events()
            assert len(events) == len({event.event_id for event in events})

        # The foreign worker still holds its actual finite physical grant.
        # A third organization must make durable progress through an
        # unrelated physical resource, on another PostgreSQL connection,
        # before that grant is released. A final-state-only assertion after
        # release would not falsify namespace-wide serialization.
        independent_errors: list[BaseException] = []
        progressed = Event()

        def advance_independent_owner():
            try:
                with PostgresPersistence(DSN, namespace=namespace) as other_store:
                    other_engine, other_backend = _rebuild(
                        other_store, owner_id=UNBLOCKED_OWNER,
                    )
                    other_backend.run_until(logistics.PICKUP_DUE)
                    assert logistics.reconcile_pickup(
                        other_store, other_engine, other_backend,
                        entities=unblocked_shipment,
                    )
                    assert logistics.reconcile_origin_hub(
                        other_store, other_engine, other_backend,
                        entities=unblocked_shipment,
                    )
                    assert other_store.entity(
                        "shipment", unblocked_shipment.shipment_id,
                    ).state == "at_origin_hub"
            except BaseException as error:
                independent_errors.append(error)
            finally:
                progressed.set()

        independent = Thread(target=advance_independent_owner, daemon=True)
        independent.start()
        assert progressed.wait(timeout=15), (
            "unrelated owner blocked behind a live finite-resource holder"
        )
        independent.join(timeout=2)
        assert not independent.is_alive()
        assert not independent_errors, repr(independent_errors)
        assert foreign.is_alive(), "physical holder released before independent progress"
        assert not released.is_set()
        with PostgresPersistence(DSN, namespace=namespace) as verify:
            assert verify.entity(
                "shipment", unblocked_shipment.shipment_id,
            ).state == "at_origin_hub"
            unrelated_events = [
                event for event in verify.events()
                if event.entity_id == unblocked_shipment.shipment_id
            ]
            assert unrelated_events
            assert not {
                event.event_id for event in unrelated_events
            }.intersection(
                event.event_id for event in verify.events()
                if event.entity_id == shipment.shipment_id
            )
    finally:
        release.set()
        foreign.join(timeout=35)

    assert not foreign.is_alive(), "second worker did not release finite resource"
    assert released.is_set()
    assert not holder_errors, repr(holder_errors)

    with PostgresPersistence(DSN, namespace=namespace) as store:
        if blocker == "destination_put":
            item_id = f"destination-queue:{shipment.shipment_id}"
            assert any(
                r.item.item_id == item_id
                and r.request_id == f"foreign-receive:{shipment.shipment_id}"
                for r in store.store_get_results()
            )
            assert not any(i.item_id == item_id for i in store.store_items())
        engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
        assert logistics.reconcile_transfer(
            store, engine, backend, entities=shipment,
        ) is True
        assert store.entity("shipment", shipment.shipment_id).state == "at_destination_hub"
        assert engine.resources.has_request(
            f"transfer-vehicle:{shipment.shipment_id}"
        ) is False
        assert engine.resources.has_request(
            f"destination-dock:{shipment.shipment_id}"
        ) is False
        assert not any(
            i.item_id == f"destination-queue:{shipment.shipment_id}"
            for i in store.store_put_intents()
        )
        canonical = _snapshot(store)
        assert len(store.events()) == len({e.event_id for e in store.events()})

    with PostgresPersistence(DSN, namespace=namespace) as store:
        engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
        assert logistics.reconcile_transfer(
            store, engine, backend, entities=shipment,
        ) is True
        assert _snapshot(store) == canonical


@pytest.mark.parametrize("blocker", ["transfer_vehicle", "destination_dock"])
def test_pg_transfer_differential_reopen_matches_continuous_physical_causal_ledger(
    blocker,
):
    """#489 bounded PG differential on the exact finite resource boundary.

    Same owner IDs, seed, exogenous arrivals, queue filler, and dispatch order
    in TWO isolated PostgreSQL namespaces. Control holds one PG connection
    throughout; recovered path reconnects after each causally meaningful
    step and rebuilds the SimPy environment from authoritative reservations.
    Unlike existing one-arm final-state checks this tests each intermediate
    source ledger and terminal after retry; OS-process SIGKILL and certified
    BusinessEffectApplied remain independent acceptance gates.
    """
    assert DSN is not None
    capacities = {"transfer_vehicle": 2} if blocker == "destination_dock" else None

    def _replay_state(store):
        # A physical-only snapshot can miss an owner clock loss after
        # reconnect: rebuilding at logistics.ORIGIN masks the divergence.
        # Read both independent positions and all durable pending work and
        # commands from the SAME authoritative PostgreSQL transaction.
        with store.transaction() as uow:
            return (
                uow.get_simulation_position(owner_id=HOLDER_OWNER),
                uow.get_simulation_position(owner_id=SHIPMENT_OWNER),
                uow.commands(),
                uow.scheduled_work(),
            )

    def run_world(*, reconnect: bool):
        namespace = "pc6_transfer_diff_" + uuid4().hex[:12]
        observations = []
        with PostgresPersistence(DSN, namespace=namespace) as persistent:
            holder = logistics.seed_reference(
                persistent, owner_id=HOLDER_OWNER,
                instance_key="differential-holder",
                resource_capacity=1, resource_capacities=capacities,
                hub_queue_capacity=1,
            )
            contender = logistics.seed_reference(
                persistent, owner_id=SHIPMENT_OWNER,
                instance_key="differential-contender",
                resource_capacity=1, resource_capacities=capacities,
                hub_queue_capacity=1,
            )

            def checkpoint(action):
                if reconnect:
                    with PostgresPersistence(DSN, namespace=namespace) as current:
                        action(current)
                        sample = (
                            _snapshot(current),
                            _replay_state(current),
                        )
                        receipts = current.business_effects()
                else:
                    action(persistent)
                    sample = (
                        _snapshot(persistent),
                        _replay_state(persistent),
                    )
                    receipts = persistent.business_effects()
                # Not a Trading Company workflow: this bounded Logistics
                # path has no certified cross-domain BusinessEffectApplied.
                assert receipts == ()
                observations.append(sample)

            def at_origin(store, *, owner, shipment):
                engine, backend = _rebuild(store, owner_id=owner)
                backend.run_until(logistics.PICKUP_DUE)
                assert logistics.reconcile_pickup(
                    store, engine, backend, entities=shipment,
                )
                assert logistics.reconcile_origin_hub(
                    store, engine, backend, entities=shipment,
                )
                assert store.entity("shipment", shipment.shipment_id).state == "at_origin_hub"

            checkpoint(lambda st: at_origin(
                st, owner=HOLDER_OWNER, shipment=holder,
            ))
            checkpoint(lambda st: at_origin(
                st, owner=SHIPMENT_OWNER, shipment=contender,
            ))

            queue = logistics._organization_queue(
                HOLDER_OWNER, "destination_hub_queue",
            )
            filler_id = "destination-queue:controlled-holder-filler"

            def occupy_real_holder(store):
                engine, backend = _rebuild(store, owner_id=HOLDER_OWNER)
                backend.run_until(logistics.PICKUP_DUE)
                engine.stores.ensure_put(
                    backend, store_name=queue, item_id=filler_id,
                    value={"shipment_id": "controlled-holder-filler"},
                    requested_at=backend.now,
                )
                backend.run_until(backend.now)
                assert logistics.reconcile_transfer(
                    store, engine, backend, entities=holder,
                ) is False
                assert store.entity("shipment", holder.shipment_id).state == "in_transfer"
                assert engine.resources.reservation_for(
                    f"transfer-vehicle:{holder.shipment_id}"
                ) is not None
                assert engine.resources.reservation_for(
                    f"destination-dock:{holder.shipment_id}"
                ) is not None

            checkpoint(occupy_real_holder)

            def contend_real_shipment(store):
                engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
                backend.run_until(logistics.PICKUP_DUE)
                assert logistics.reconcile_transfer(
                    store, engine, backend, entities=contender,
                ) is False
                assert engine.resources.has_request(
                    f"{'transfer-vehicle' if blocker == 'transfer_vehicle' else 'destination-dock'}:{contender.shipment_id}"
                )
                assert store.entity("shipment", contender.shipment_id).state in (
                    "at_origin_hub", "in_transfer",
                )

            checkpoint(contend_real_shipment)

            def release_holder(store):
                engine, backend = _rebuild(store, owner_id=HOLDER_OWNER)
                backend.run_until(logistics.PICKUP_DUE)
                selected = engine.stores.ensure_selection(
                    backend, store_name=queue,
                    request_id="differential-holder-dequeue",
                    requested_at=backend.now,
                )
                assert selected is not None and selected.item.item_id == filler_id
                backend.run_until(backend.now)
                assert logistics.reconcile_transfer(
                    store, engine, backend, entities=holder,
                )
                assert store.entity("shipment", holder.shipment_id).state == (
                    "at_destination_hub"
                )

            checkpoint(release_holder)

            def complete_contender(store):
                engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
                backend.run_until(logistics.PICKUP_DUE)
                assert logistics.reconcile_transfer(
                    store, engine, backend, entities=contender,
                )
                assert store.entity("shipment", contender.shipment_id).state == (
                    "at_destination_hub"
                )
                reservations = store.resource_reservations()
                for name in ("transfer_vehicle", "destination_dock"):
                    assert not [x for x in reservations if x.resource_name == name]
                assert len(store.events()) == len({
                    x.event_id for x in store.events()
                })

            checkpoint(complete_contender)
            # Retry must not append a second event/effect or double-release.
            checkpoint(complete_contender)

        return tuple(observations)

    uninterrupted = run_world(reconnect=False)
    recovered = run_world(reconnect=True)
    assert len(uninterrupted) == len(recovered) == 7
    for stage, (continuous, restarted) in enumerate(zip(uninterrupted, recovered)):
        assert restarted == continuous, (
            f"PostgreSQL differential physical/causal drift after stage {stage}: "
            f"{blocker}"
        )

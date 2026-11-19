"""#590: falsify the PC6 customer composition causal/physical guard edges.

These tests exercise business constraints without substituting an integration
mock for the production path. Stubs are used only at I/O boundaries so that
owner and event causality, physical wait classification and clock gating remain
observable and deterministic. They do not claim durable PostgreSQL recovery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sose.composition import trading_company_customer as customer
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 10, 25, 8, tzinfo=timezone.utc)
OWNER = "org:customer-coverage"
SHIPMENT = "shipment:coverage"


@dataclass
class _ClockStore:
    logical_time: datetime | None = None

    def simulation_position(self, *, owner_id):
        assert owner_id == OWNER
        return (
            SimpleNamespace(logical_time=self.logical_time)
            if self.logical_time is not None else None
        )


@pytest.mark.parametrize(
    "candidate_clock, expected",
    [
        (None, NOW + timedelta(minutes=1)),
        (NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)),
        (NOW + timedelta(minutes=5), NOW + timedelta(minutes=5)),
    ],
)
def test_next_composition_tick_obeys_durable_clock(candidate_clock, expected):
    store = _ClockStore(candidate_clock)
    assert customer._next_logical_time(
        store, NOW, organization_id=OWNER,
    ) == expected


@pytest.mark.parametrize(
    "committed_clock, requested, expected",
    [
        (None, NOW, NOW),
        (NOW, NOW, NOW),
        (NOW - timedelta(minutes=1), NOW, NOW - timedelta(minutes=1)),
    ],
)
def test_recovery_clock_is_reconstructed_from_owner_position(
    committed_clock, requested, expected,
):
    store = _ClockStore(committed_clock)
    assert customer._recovery_time(
        store, requested, organization_id=OWNER,
    ) == expected


def test_recovery_clock_rejects_backdating_behind_committed_owner_time():
    store = _ClockStore(NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cannot precede committed logical time"):
        customer._recovery_time(store, NOW, organization_id=OWNER)


@pytest.mark.parametrize("correlation", (None, "", "  ", 123))
def test_egress_never_infers_owner_from_invalid_correlation(correlation):
    with pytest.raises(ValueError, match="correlation_id must not be empty"):
        customer._operational_owner({"corr": OWNER}, correlation)


@pytest.mark.parametrize("owners", ({}, {"corr": ""}, {"corr": " "}, {"corr": 123}))
def test_egress_requires_explicit_nonempty_owner_binding(owners):
    with pytest.raises(ValueError, match="explicit correlation-to-organization"):
        customer._operational_owner(owners, "corr")


def test_egress_resolves_exact_owner_binding_without_guessing():
    assert customer._operational_owner({"corr": OWNER}, "corr") == OWNER


@pytest.mark.parametrize(
    "pending, granted, expected_error",
    [
        (False, False, RuntimeError),
        (False, True, RuntimeError),
        (True, True, RuntimeError),
        (True, False, customer.DurableResourceWait),
    ],
)
def test_delivery_failure_is_transient_only_for_ungranted_durable_courier_demand(
    pending, granted, expected_error,
):
    ordinal = 1
    attempt = customer.logistics.delivery_attempt_id(
        ordinal, shipment_id=SHIPMENT,
    )
    request_id = f"delivery-courier:{attempt}"
    store = SimpleNamespace(
        resource_demands=lambda: (
            (SimpleNamespace(
                request_id=request_id, resource_name="delivery_courier",
            ),) if pending else ()
        ),
        resource_reservations=lambda: (
            (SimpleNamespace(request_id=request_id),) if granted else ()
        ),
    )
    with pytest.raises(expected_error) as exc:
        customer._raise_delivery_dispatch_failure_or_wait(
            store, shipment_id=SHIPMENT, ordinal=ordinal,
        )
    if pending and not granted:
        assert isinstance(exc.value, customer.DurableResourceWait)
        assert exc.value.request_id == request_id
        assert exc.value.resource_name == "delivery_courier"
    else:
        assert str(exc.value) == "logistics delivery dispatch failed"


def test_another_resource_demand_cannot_masquerade_as_courier_wait():
    attempt = customer.logistics.delivery_attempt_id(1, shipment_id=SHIPMENT)
    store = SimpleNamespace(
        resource_demands=lambda: (
            SimpleNamespace(
                request_id=f"delivery-courier:{attempt}",
                resource_name="unrelated_pool",
            ),
        ),
        resource_reservations=lambda: (),
    )
    with pytest.raises(RuntimeError, match="logistics delivery dispatch failed"):
        customer._raise_delivery_dispatch_failure_or_wait(
            store, shipment_id=SHIPMENT, ordinal=1,
        )


@pytest.mark.parametrize("claimed_id", (None, "foreign", "wanted"))
def test_boundary_consumption_never_acks_missing_or_foreign_lease(claimed_id):
    consumed = []
    claimed = []
    class _Service:
        def claim_next(self, **kwargs):
            claimed.append(kwargs)
            return (
                SimpleNamespace(message_id=claimed_id)
                if claimed_id is not None else None
            )

        def consume(self, **kwargs):
            consumed.append(kwargs)
            return SimpleNamespace(consumer_effect_id="effect:accepted")

    svc = _Service()
    if claimed_id != "wanted":
        error = (
            "expected own pending composition delivery"
            if claimed_id is None else "composition claimed another causal flow"
        )
        with pytest.raises(RuntimeError, match=error):
            customer._consume_next(
                svc, object(), owner_id=OWNER, now=NOW,
                expected_message_id="wanted",
            )
        assert consumed == []
    else:
        registry = object()
        assert customer._consume_next(
            svc, registry, owner_id=OWNER, now=NOW,
            expected_message_id="wanted",
        ) == "effect:accepted"
        assert consumed[0]["registry"] is registry
    assert claimed[0]["message_id"] == "wanted"
    assert claimed[0]["owner_id"] == OWNER
    assert claimed[0]["lease_duration"] == timedelta(hours=1)


@pytest.mark.parametrize("case", ("missing_owner", "missing_shipment"))
def test_pickup_fails_closed_before_finite_capacity_admission(case):
    intent = SimpleNamespace(entity_id=SHIPMENT, due_at=NOW)
    store = SimpleNamespace(
        entity=lambda _kind, _id: None,
        simulation_position=lambda **_kwargs: None,
    )
    with pytest.raises(
        ValueError if case == "missing_owner" else RuntimeError,
        match=("organization_id must not be empty"
               if case == "missing_owner" else "missing shipment"),
    ):
        customer.pickup_resource_due_at(
            store, intent,
            organization_id=" " if case == "missing_owner" else OWNER,
        )


@pytest.mark.parametrize(
    "state, clock, scheduled, expected",
    [
        ("created", None, (), NOW + timedelta(hours=1)),
        ("created", NOW + timedelta(hours=2), (), NOW + timedelta(hours=3)),
        # These are actual ShipmentChart states: a pickup is already
        # scheduled or temporarily delayed, but not yet consummated.
        ("pickup_scheduled", None, (), NOW),
        ("delayed_pickup", NOW + timedelta(hours=2), (), NOW + timedelta(hours=2)),
        (
            "pickup_scheduled", None,
            (
                # An *earlier* valid-target but wrong-action command must
                # remain ineligible; a separately foreign-target command
                # isolates the identity predicate. Mutating either predicate
                # must change the selected time and fail this regression.
                ("wrong-action", NOW + timedelta(minutes=4), SHIPMENT, "pickup"),
                ("valid-late", NOW + timedelta(minutes=30), SHIPMENT, "schedule_pickup"),
                ("valid-soon", NOW + timedelta(minutes=10), SHIPMENT, "schedule_pickup"),
                ("wrong-target", NOW, "other", "schedule_pickup"),
            ),
            NOW + timedelta(minutes=10),
        ),
    ],
)
def test_pickup_resolves_only_matching_scheduled_causal_work(
    state, clock, scheduled, expected,
):
    known = {
        key: SimpleNamespace(
            due_at=at, name=action, entity_type="shipment", entity_id=target,
        )
        for key, at, target, action in scheduled
    }
    store = SimpleNamespace(
        entity=lambda _type, _id: SimpleNamespace(id=SHIPMENT, state=state),
        simulation_position=lambda **_kwargs: (
            None if clock is None else SimpleNamespace(logical_time=clock)
        ),
        scheduled_work=lambda: tuple(
            SimpleNamespace(command_id=key) for key in known
        ),
        command=lambda key: known.get(key),
    )
    assert customer.pickup_resource_due_at(
        store, SimpleNamespace(entity_id=SHIPMENT, due_at=NOW),
        organization_id=OWNER,
    ) == expected


@pytest.mark.parametrize(
    "owner, persistence, instance",
    [
        ("", None, None),
        (" ", None, None),
        (None, None, None),
        (OWNER, "injected_store", None),
        (OWNER, None, ""),
        (OWNER, None, 42),
    ],
)
def test_customer_runner_rejects_unbound_identity_before_creating_state(
    owner, persistence, instance,
):
    with pytest.raises(ValueError, match="organization_id|instance_key"):
        customer.run_customer_demand_path(
            organization_id=owner,
            persistence=(MemoryPersistence() if persistence == "injected_store" else None),
            instance_key=instance,
        )


def test_command_provenance_scope_restores_original_factory_on_exception():
    original = SimpleNamespace(
        create=lambda name, **kwargs: (name, kwargs),
    )
    context = SimpleNamespace(commands=original)
    engine = SimpleNamespace(context=context)
    issuer = object()
    with pytest.raises(RuntimeError, match="forced rollback"):
        with customer._causal_command_scope(
            engine, intent=issuer, correlation_id="corr:canonical",
        ):
            name, kwargs = context.commands.create(
                "send", correlation_id="forged", caused_by=object(),
            )
            assert name == "send"
            assert kwargs["correlation_id"] == "corr:canonical"
            assert kwargs["caused_by"] is issuer
            raise RuntimeError("forced rollback")
    assert context.commands is original


def test_durable_intent_handler_rejects_missing_owner_and_conflicting_payload():
    with pytest.raises(ValueError, match="operational owner_id"):
        customer._intent_handler(
            intent_name="composition.coverage", entity_type="shipment",
            entity_id=SHIPMENT, owner_id=" ",
        )

    store = MemoryPersistence()
    handler = customer._intent_handler(
        intent_name="composition.coverage", entity_type="shipment",
        entity_id=SHIPMENT, owner_id=OWNER,
    )
    def boundary_message(payload):
        return SimpleNamespace(
            message_id="message:fixed", correlation_id="correlation:fixed",
            contract_key="shipment.created.v1", produced_at=NOW,
            payload=lambda: dict(payload),
        )

    with store.transaction() as uow:
        canonical = handler(boundary_message({"business_key": "original"}), uow)
        assert handler(boundary_message({"business_key": "original"}), uow) == canonical
        command = uow.get_command(canonical)
        assert command is not None
        assert command.owner_id == OWNER
        assert command.causation_id == "message:fixed"
        assert command.payload["boundary_message_id"] == "message:fixed"
        with pytest.raises(ValueError, match="identity conflict"):
            handler(boundary_message({"business_key": "altered"}), uow)
        assert uow.get_command(canonical) == command
    assert store.command(canonical) == command

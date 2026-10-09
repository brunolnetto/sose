"""A settlement destination must be a durable identity, never an amount match."""
from dataclasses import replace

import pytest

from sose.composition.bindings import CustomerSettlementBinding, SettlementBindingService
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def entities(store, *, order, payment, journal, amount=250.0, currency="USD"):
    with store.transaction() as uow:
        for entity_type, entity_id in (
            ("sales_order", order), ("card_payment", payment), ("journal_entry", journal),
        ):
            uow.save_entity(Entity(
                id=entity_id, entity_type=entity_type, state="submitted",
                attributes={"amount": amount, "currency": currency},
            ))


def binding(order="order-1", payment="card-1", journal="journal-1", amount=250.0):
    return CustomerSettlementBinding.create(
        order_id=order, payment_id=payment, journal_id=journal,
        amount=amount, currency="USD", correlation_id="flow:" + order,
    )


def test_durable_binding_retrieves_all_three_identical_authoritative_identities():
    store = MemoryPersistence()
    entities(store, order="order-1", payment="card-1", journal="journal-1")
    service = SettlementBindingService(store)
    saved = service.bind(binding())
    assert service.bind(binding()) == saved
    assert service.for_order("order-1") == saved
    assert service.for_payment("card-1") == saved
    assert service.for_journal("journal-1") == saved
    assert service.for_order("unknown") is None
    with store.transaction() as uow:
        assert uow.get_customer_settlement_binding("order", saved.order_id) == saved
        assert uow.get_customer_settlement_binding("payment", saved.payment_id) == saved
        assert uow.get_customer_settlement_binding("journal", saved.journal_id) == saved


@pytest.mark.parametrize("collision", ["order", "payment", "journal"])
def test_cannot_reassign_a_durable_settlement_identity(collision):
    store = MemoryPersistence()
    entities(store, order="o1", payment="p1", journal="j1")
    entities(store, order="o2", payment="p2", journal="j2")
    service = SettlementBindingService(store)
    service.bind(binding("o1", "p1", "j1"))
    candidate = {"order": "o2", "payment": "p2", "journal": "j2"}
    if collision == "order":
        candidate["order"] = "o1"
    elif collision == "payment":
        candidate["payment"] = "p1"
    else:
        candidate["journal"] = "j1"
    # No physical foreign-key mixups are allowed, even if candidate values match.
    with pytest.raises(ValueError, match="already bound"):
        service.bind(binding(candidate["order"], candidate["payment"], candidate["journal"]))
    assert service.for_order("o1") == binding("o1", "p1", "j1")
    assert service.for_order("o2") is None


@pytest.mark.parametrize("mutation", [
    {"amount": 999}, {"currency": "EUR"}, {"order_id": "not-found"},
])
def test_binding_fails_closed_for_unverified_materialized_endpoints(mutation):
    store = MemoryPersistence()
    entities(store, order="order-1", payment="card-1", journal="journal-1")
    bad = replace(binding(), **mutation)
    with pytest.raises(ValueError, match="missing|amount|currency"):
        SettlementBindingService(store).bind(bad)
    assert SettlementBindingService(store).for_order("order-1") is None


@pytest.mark.parametrize("missing", ["order_id", "payment_id", "journal_id", "currency", "correlation_id"])
def test_binding_requires_nonempty_source_identity(missing):
    with pytest.raises(ValueError, match="identity"):
        replace(binding(), **{missing: ""})


@pytest.mark.parametrize("amount", [-1.0, 0, float("nan"), float("inf")])
def test_binding_rejects_invalid_amount(amount):
    with pytest.raises(ValueError, match="amount"):
        replace(binding(), amount=amount)


def test_identical_amounts_are_not_a_join_key_and_survive_restart(tmp_path):
    path = tmp_path / "bindings.sqlite"
    with SQLiteIncrementalPersistence(path) as writer:
        entities(writer, order="o1", payment="p1", journal="j1")
        entities(writer, order="o2", payment="p2", journal="j2")
        service = SettlementBindingService(writer)
        service.bind(binding("o1", "p1", "j1"))
        service.bind(binding("o2", "p2", "j2"))
    with SQLiteIncrementalPersistence(path) as store:
        resolver = SettlementBindingService(store)
        assert resolver.for_order("o1").payment_id == "p1"
        assert resolver.for_order("o2").payment_id == "p2"
        assert resolver.for_payment("p2").journal_id == "j2"
        assert resolver.for_journal("j1").order_id == "o1"


def test_two_open_adapters_observe_immutable_binding_without_stale_snapshot(tmp_path):
    path = tmp_path / "binding-adapter-revision.sqlite"
    with (
        SQLiteIncrementalPersistence(path) as publisher,
        SQLiteIncrementalPersistence(path) as observer,
    ):
        entities(publisher, order="o1", payment="p1", journal="j1")
        receiver = SettlementBindingService(observer)
        assert receiver.for_payment("p1") is None
        written = SettlementBindingService(publisher).bind(binding("o1", "p1", "j1"))
        assert receiver.for_payment("p1") == written

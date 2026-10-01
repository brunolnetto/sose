from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.warehouse import DomainApplyResult, DomainMutation, MemoryDomainWarehouse
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def mutation(state: str = "released") -> DomainMutation:
    return DomainMutation(
        "mro:wo-1:release",
        Entity(id="wo-1", entity_type="work_order", state=state),
    )


def test_prepare_is_atomic_with_engine_oltp_and_flushes_to_warehouse():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    outbox = DomainMutationOutbox(engine, warehouse)

    delivery = outbox.prepare(mutation())
    assert engine.domain_delivery(delivery.mutation_id) == delivery
    assert warehouse.entity("work_order", "wo-1") is None

    assert outbox.flush() == 1
    assert engine.domain_deliveries() == ()
    assert warehouse.entity("work_order", "wo-1").state == "released"


def test_restart_recovers_pending_domain_delivery(tmp_path):
    path = tmp_path / "engine.db"
    first = SQLiteIncrementalPersistence(path)
    DomainMutationOutbox(first, MemoryDomainWarehouse()).prepare(mutation())
    first.close()

    reopened = SQLiteIncrementalPersistence(path)
    warehouse = MemoryDomainWarehouse()
    outbox = DomainMutationOutbox(reopened, warehouse)
    assert [item.mutation_id for item in outbox.pending()] == ["mro:wo-1:release"]
    assert outbox.flush() == 1
    assert warehouse.entity("work_order", "wo-1").state == "released"
    reopened.close()


def test_crash_after_warehouse_apply_before_ack_is_safe():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    outbox = DomainMutationOutbox(engine, warehouse)
    delivery = outbox.prepare(mutation())

    # Simulate process death after the external commit but before Engine OLTP ACK.
    assert warehouse.apply(delivery.mutation) is DomainApplyResult.APPLIED
    assert engine.domain_delivery(delivery.mutation_id) is not None

    # A fresh outbox replays the same immutable mutation and only then ACKs it.
    restarted = DomainMutationOutbox(engine, warehouse)
    assert restarted.deliver(delivery) is DomainApplyResult.REPLAYED
    assert engine.domain_delivery(delivery.mutation_id) is None
    assert warehouse.entity("work_order", "wo-1").state == "released"


def test_prepare_rejects_conflicting_mutation_identity():
    engine = MemoryPersistence()
    outbox = DomainMutationOutbox(engine, MemoryDomainWarehouse())
    outbox.prepare(mutation("released"))
    try:
        outbox.prepare(mutation("cancelled"))
    except ValueError as exc:
        assert "identity conflict" in str(exc)
    else:
        raise AssertionError("conflicting domain mutation identity was accepted")

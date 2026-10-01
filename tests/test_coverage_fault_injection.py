from __future__ import annotations

import builtins
import runpy
from types import SimpleNamespace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p import simulation as p2p
from sose.examples.p2p.simulation import P2PEntities


def test_simpy_backend_reports_missing_optional_dependency(monkeypatch):
    original_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "simpy" or name.startswith("simpy."):
            raise ImportError("simpy intentionally unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(ImportError, match="SimPyBackend requires the optional 'simpy' dependency"):
        runpy.run_module("sose.backends.simpy", run_name="__sose_missing_simpy__")


def test_simpy_store_rejects_malformed_native_value():
    runtime = SimPyBackend(origin=p2p.ORIGIN)
    runtime.create_store("fault-injected")

    # Deliberately bypass the public adapter boundary to validate the defensive
    # conversion guard against corrupt/native SimPy values.
    runtime._stores["fault-injected"].store.put(object())

    runtime.get_store(
        "fault-injected",
        request_id="malformed",
        on_received=lambda item: None,
    )

    with pytest.raises(RuntimeError, match="store returned a non-SOSE item"):
        runtime.run_until(runtime.now)


def _ids() -> P2PEntities:
    return P2PEntities(
        requisition_id="req",
        purchase_order_id="po",
        receipt_id="receipt",
        material_demand_id="demand",
    )


class _Backend:
    now = p2p.ORIGIN

    def run_until(self, at):
        assert at == self.now
        return 0


def test_receiving_rejects_missing_seeded_receipt():
    persistence = SimpleNamespace(entity=lambda kind, entity_id: None)

    with pytest.raises(RuntimeError, match="receipt was not persisted"):
        p2p.reconcile_receiving_resources(
            persistence,
            object(),
            object(),
            entities=_ids(),
        )


def test_stocking_rejects_missing_receipt_after_durable_inventory_effects():
    persistence = SimpleNamespace(
        store_items=lambda: (SimpleNamespace(item_id="receipt-lot-1"),),
        store_put_intents=lambda: (),
        store_get_results=lambda: (),
        container_operation_results=lambda: (
            SimpleNamespace(request_id="stock-receipt-1"),
        ),
        container_operation_intents=lambda: (),
        entity=lambda kind, entity_id: None,
    )

    with pytest.raises(RuntimeError, match="receipt was not persisted"):
        p2p.reconcile_stocking(
            persistence,
            object(),
            _Backend(),
            entities=_ids(),
            quantity=1.0,
        )


def test_consumption_rejects_missing_material_demand_after_withdrawal():
    persistence = SimpleNamespace(
        store_get_requests=lambda: (
            SimpleNamespace(request_id="consume-receipt-lot-1"),
        ),
        store_get_results=lambda: (
            SimpleNamespace(request_id="consume-receipt-lot-1"),
        ),
        container_operation_intents=lambda: (
            SimpleNamespace(request_id="consume-demand-1"),
        ),
        container_operation_results=lambda: (
            SimpleNamespace(request_id="consume-demand-1"),
        ),
        entity=lambda kind, entity_id: None,
    )

    with pytest.raises(RuntimeError, match="material demand was not persisted"):
        p2p.reconcile_consumption(
            persistence,
            object(),
            _Backend(),
            entities=_ids(),
            quantity=1.0,
        )


def test_shortage_runner_rejects_missing_seeded_material_demand(monkeypatch):
    persistence = SimpleNamespace(entity=lambda kind, entity_id: None)
    engine = SimpleNamespace(rebuild_backend=lambda backend: None)

    monkeypatch.setattr(p2p, "MemoryPersistence", lambda: persistence)
    monkeypatch.setattr(p2p, "seed_happy_path", lambda persistence, quantity: _ids())
    monkeypatch.setattr(p2p, "build_runtime", lambda persistence: (object(), engine))
    monkeypatch.setattr(p2p, "SimPyBackend", lambda origin: object())

    with pytest.raises(RuntimeError, match="material demand was not persisted"):
        p2p.run_shortage_backorder(quantity=1.0)

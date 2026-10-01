from __future__ import annotations

import subprocess
import sys

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p import simulation as p2p
from sose.persistence.memory import MemoryPersistence


def test_simpy_optional_dependency_failure_is_user_visible():
    script = r"""
import importlib.abc
import sys

class BlockSimPy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "simpy" or fullname.startswith("simpy."):
            raise ModuleNotFoundError("blocked simpy for packaging test")
        return None

sys.meta_path.insert(0, BlockSimPy())
for name in list(sys.modules):
    if name == "simpy" or name.startswith("simpy."):
        del sys.modules[name]

import sose.backends.simpy  # noqa: F401
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "SimPyBackend requires the optional 'simpy' dependency" in completed.stderr


def test_simpy_store_rejects_non_sose_native_item():
    backend = SimPyBackend(origin=p2p.ORIGIN)
    backend.create_store("items")
    backend._stores["items"].store.items.append(object())

    backend.get_store(
        "items",
        request_id="bad-native",
        on_received=lambda item: None,
    )

    with pytest.raises(RuntimeError, match="store returned a non-SOSE item"):
        while backend.step():
            pass


def _fresh_runtime():
    persistence = MemoryPersistence()
    entities = p2p.seed_happy_path(persistence)
    _, engine = p2p.build_runtime(persistence)
    backend = SimPyBackend(origin=p2p.ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _hide_entity(monkeypatch, persistence, entity_type: str, entity_id: str):
    original = persistence.entity

    def hidden(current_type: str, current_id: str):
        if current_type == entity_type and current_id == entity_id:
            return None
        return original(current_type, current_id)

    monkeypatch.setattr(persistence, "entity", hidden)


def test_p2p_receiving_detects_missing_seeded_receipt(monkeypatch):
    persistence, entities, engine, backend = _fresh_runtime()
    _hide_entity(monkeypatch, persistence, "receipt", entities.receipt_id)

    with pytest.raises(RuntimeError, match="receipt was not persisted"):
        p2p.reconcile_receiving_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_p2p_stocking_detects_missing_seeded_receipt(monkeypatch):
    persistence, entities, engine, backend = _fresh_runtime()
    _hide_entity(monkeypatch, persistence, "receipt", entities.receipt_id)

    with pytest.raises(RuntimeError, match="receipt was not persisted"):
        p2p.reconcile_stocking(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_p2p_consumption_detects_missing_material_demand(monkeypatch):
    persistence, entities = p2p.run_happy_path()
    _, engine = p2p.build_runtime(persistence)
    backend = SimPyBackend(origin=p2p.ORIGIN)
    engine.rebuild_backend(backend)
    _hide_entity(
        monkeypatch,
        persistence,
        "material_demand",
        entities.material_demand_id,
    )

    with pytest.raises(RuntimeError, match="material demand was not persisted"):
        p2p.reconcile_consumption(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_p2p_shortage_detects_missing_seeded_material_demand(monkeypatch):
    original_seed = p2p.seed_happy_path

    def corrupt_seed(persistence, *, quantity=5.0):
        entities = original_seed(persistence, quantity=quantity)
        _hide_entity(
            monkeypatch,
            persistence,
            "material_demand",
            entities.material_demand_id,
        )
        return entities

    monkeypatch.setattr(p2p, "seed_happy_path", corrupt_seed)

    with pytest.raises(RuntimeError, match="material demand was not persisted"):
        p2p.run_shortage_backorder()

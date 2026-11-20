"""#629: the OD-2/3/4 module paths deprecated in 0.11.0 are removed in 0.12.0 (ADR-0011).

Two old paths stay permanently, serving only the persisted entity class that stored
records name by that path (codec class identifiers).
"""
from __future__ import annotations

import importlib
import warnings

import pytest

from sose.persistence.codec import dumps, loads

REMOVED = [
    "od2_actor_time_ledger", "od3_decisions", "od3_queue_ledger",
    "od3_queue_runtime", "od4_command_lineage",
]
PERSISTED = {
    ("od2_actor", "actor_lifecycle"): "OrganizationalActor",
    ("od4_intake", "work_intake"): "ManagementWork",
}


@pytest.mark.parametrize("old", REMOVED)
def test_removed_module_paths_no_longer_import(old):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"sose.organizational.{old}")


@pytest.mark.parametrize(("old", "name", "former_api"), [
    ("od2_actor", "OrganizationalActor", "seed_actor"),
    ("od4_intake", "ManagementWork", "admit_work"),
])
def test_persisted_paths_serve_only_their_entity_class(old, name, former_api):
    module = importlib.import_module(f"sose.organizational.{old}")
    assert module.__all__ == [name]
    with pytest.raises(ImportError):
        exec(f"from sose.organizational.{old} import {former_api}", {})


@pytest.mark.parametrize(("paths", "name"), sorted(PERSISTED.items()))
def test_persisted_entity_classes_keep_their_codec_class_path(paths, name):
    old, new = paths
    cls = getattr(importlib.import_module(f"sose.organizational.{new}"), name)
    payload = dumps(cls(id="entity-1"))
    assert f'"class":"sose.organizational.{old}:{name}"' in payload
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        old_class = getattr(importlib.import_module(f"sose.organizational.{old}"), name)
        restored = loads(payload)
    assert old_class is cls
    assert type(restored) is cls and restored == cls(id="entity-1")

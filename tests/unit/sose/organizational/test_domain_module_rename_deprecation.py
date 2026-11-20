"""#629: OD-2/3/4 phase-numbered modules moved to domain names (ADR-0011).

The old paths stay importable for one cycle (deprecated in 0.11.0, removed in 0.12.0):
every name resolves to the same object as the new module and warns at the caller.
Persisted entity classes keep their codec class path and resolve without a warning.
"""
from __future__ import annotations

import importlib
import warnings

import pytest

from sose.persistence.codec import dumps, loads

RENAMES = {
    "od2_actor": "actor_lifecycle",
    "od2_actor_time_ledger": "actor_time_ledger",
    "od3_decisions": "queue_decisions",
    "od3_queue_ledger": "queue_policy_ledger",
    "od3_queue_runtime": "queue_policy_runtime",
    "od4_command_lineage": "work_command_lineage",
    "od4_intake": "work_intake",
}
PERSISTED_CLASSES = {
    "od2_actor": "OrganizationalActor",
    "od4_intake": "ManagementWork",
}


def _public_names(module):
    return [
        name for name, value in vars(module).items()
        if not name.startswith("_") and getattr(value, "__module__", None) in (
            module.__name__, *(f"sose.organizational.{old}" for old in PERSISTED_CLASSES),
        )
    ]


@pytest.mark.parametrize(("old", "new"), sorted(RENAMES.items()))
def test_old_module_path_warns_and_resolves_to_the_new_object(old, new):
    old_module = importlib.import_module(f"sose.organizational.{old}")
    new_module = importlib.import_module(f"sose.organizational.{new}")
    names = [n for n in _public_names(new_module) if n != PERSISTED_CLASSES.get(old)]
    assert names
    for name in names:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            value = getattr(old_module, name)
        assert value is getattr(new_module, name)
        [warning] = caught
        assert issubclass(warning.category, DeprecationWarning)
        assert str(warning.message) == (
            f"importing {name} from sose.organizational.{old} is deprecated since SOSE "
            f"0.11.0 and will be removed in SOSE 0.12.0; import it from "
            f"sose.organizational.{new}."
        )
        assert warning.filename == __file__


def test_from_import_through_the_old_path_warns():
    with pytest.warns(DeprecationWarning, match="import it from sose.organizational.actor_lifecycle"):
        from sose.organizational.od2_actor import seed_actor
    from sose.organizational.actor_lifecycle import seed_actor as current

    assert seed_actor is current


@pytest.mark.parametrize(("old", "name"), sorted(PERSISTED_CLASSES.items()))
def test_persisted_entity_classes_keep_their_codec_class_path(old, name):
    new_module = importlib.import_module(f"sose.organizational.{RENAMES[old]}")
    cls = getattr(new_module, name)
    payload = dumps(cls(id="entity-1"))
    assert f'"class":"sose.organizational.{old}:{name}"' in payload
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        old_class = getattr(importlib.import_module(f"sose.organizational.{old}"), name)
        restored = loads(payload)
    assert old_class is cls
    assert type(restored) is cls and restored == cls(id="entity-1")


def test_unknown_names_on_old_paths_still_raise_attribute_error():
    old_module = importlib.import_module("sose.organizational.od3_decisions")
    with pytest.raises(AttributeError, match="has no attribute 'missing'"):
        old_module.missing

"""ADR-0011 deprecated re-export helper, and the completed #632 removal."""
from __future__ import annotations

import importlib
import types
import warnings

import pytest

from sose._deprecation import deprecated_reexports


def _module_with(moved):
    module = types.ModuleType("sose_test_old_home")
    module.__getattr__ = deprecated_reexports(
        module.__name__, moved, since="0.9.0", removed_in="0.10.0",
    )
    return module


def test_old_path_returns_the_same_object_and_warns_at_the_caller():
    old = _module_with({"deterministic_id": "sose.core.identity"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = old.deterministic_id
    from sose.core.identity import deterministic_id

    assert value is deterministic_id
    [warning] = caught
    assert issubclass(warning.category, DeprecationWarning)
    assert str(warning.message) == (
        "importing deterministic_id from sose_test_old_home is deprecated since SOSE 0.9.0 "
        "and will be removed in SOSE 0.10.0; import it from sose.core.identity."
    )
    assert warning.filename == __file__


def test_unknown_names_still_raise_attribute_error():
    old = _module_with({})
    with pytest.raises(AttributeError, match="has no attribute 'missing'"):
        old.missing
    assert not hasattr(old, "missing")


def test_split_reexports_were_removed_in_0_10():
    """The 0.9 deprecation cycle (#632) completed: old paths no longer resolve."""
    with pytest.raises(ImportError):
        from sose.examples.field_service.simulation import select_technician  # noqa: F401
    from sose.examples.field_service.scheduling import select_technician  # noqa: F401

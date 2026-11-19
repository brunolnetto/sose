"""#634: the legacy OD-2 audit module is deprecated in 0.11.0 (removed in 0.12.0)."""
from __future__ import annotations

import importlib
import sys
import warnings

import pytest

MODULE = "sose.organizational.od2_legacy_audit"


def test_importing_the_legacy_audit_module_warns():
    sys.modules.pop(MODULE, None)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        with pytest.warns(DeprecationWarning) as record:
            importlib.import_module(MODULE)
    (message,) = [str(w.message) for w in record if MODULE in str(w.message)]
    assert "deprecated since SOSE 0.11.0" in message
    assert "removed in SOSE 0.12.0" in message
    assert "seed_actor(tracked_time=True)" in message

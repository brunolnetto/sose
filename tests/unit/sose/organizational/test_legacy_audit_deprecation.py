"""#634: the legacy OD-2 audit module is deprecated in 0.11.0 (removed in 0.12.0)."""
from __future__ import annotations

import sys
import warnings

import pytest

MODULE = "sose.organizational.od2_legacy_audit"


def test_importing_the_legacy_audit_module_warns_at_the_import(monkeypatch):
    monkeypatch.delitem(sys.modules, MODULE, raising=False)  # restored afterwards
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        with pytest.warns(DeprecationWarning) as record:
            __import__(MODULE)  # a plain import statement, as user code would write
    (warning,) = [w for w in record if MODULE in str(w.message)]
    # Attributed to the importing code, so Python's default filters show it there.
    assert warning.filename == __file__
    message = str(warning.message)
    assert "deprecated since SOSE 0.11.0" in message
    assert "removed in SOSE 0.12.0" in message
    assert "seed_actor(tracked_time=True)" in message

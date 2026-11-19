"""Deprecated alias of :mod:`sose.organizational.work_command_lineage` (#629).

Deprecated in SOSE 0.11.0 and removed in 0.12.0 (ADR-0011): import from
``sose.organizational.work_command_lineage`` instead.
"""
from __future__ import annotations

from sose._deprecation import deprecated_reexports
from sose.organizational import work_command_lineage as _new

__getattr__ = deprecated_reexports(
    __name__,
    {name: _new.__name__ for name in vars(_new)
     if not name.startswith("__")},
    since="0.11.0", removed_in="0.12.0",
)

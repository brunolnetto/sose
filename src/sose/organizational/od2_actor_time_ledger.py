"""Deprecated alias of :mod:`sose.organizational.actor_time_ledger` (#629).

Deprecated in SOSE 0.11.0 and removed in 0.12.0 (ADR-0011): import from
``sose.organizational.actor_time_ledger`` instead.
"""
from __future__ import annotations

from sose._deprecation import deprecated_reexports
from sose.organizational import actor_time_ledger as _new

__getattr__ = deprecated_reexports(
    __name__,
    {name: _new.__name__ for name in vars(_new)
     if not name.startswith("__")},
    since="0.11.0", removed_in="0.12.0",
)

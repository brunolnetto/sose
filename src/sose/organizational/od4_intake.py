"""Deprecated alias of :mod:`sose.organizational.work_intake` (#629).

Deprecated in SOSE 0.11.0 and removed in 0.12.0 (ADR-0011): import from
``sose.organizational.work_intake`` instead.

Exception: persisted entity records name their class by this module path
(``sose.organizational.od4_intake:<class>``), so those classes keep this module as
their ``__module__`` and stay importable here without a warning. They are codec
identifiers (ADR-0011 point 5) and are not removed with the other names.
"""
from __future__ import annotations

from sose._deprecation import deprecated_reexports
from sose.organizational import work_intake as _new
from sose.organizational.work_intake import ManagementWork  # noqa: F401  (codec class paths)

__getattr__ = deprecated_reexports(
    __name__,
    {name: _new.__name__ for name in vars(_new)
     if not name.startswith("__") and name not in ('ManagementWork',)},
    since="0.11.0", removed_in="0.12.0",
)

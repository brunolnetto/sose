"""Persisted codec path of ``ManagementWork`` (#629); the module itself moved.

The code lives in :mod:`sose.organizational.work_intake`. Stored entity records name
their class ``sose.organizational.od4_intake:ManagementWork``, so this module keeps serving
that one class permanently (ADR-0011 point 5). Every other name it used to
re-export was deprecated in 0.11.0 and removed in 0.12.0: import from
``sose.organizational.work_intake``.
"""
from __future__ import annotations

from sose.organizational.work_intake import ManagementWork

__all__ = ["ManagementWork"]

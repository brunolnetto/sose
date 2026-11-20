"""Persisted codec path of ``OrganizationalActor`` (#629); the module itself moved.

The code lives in :mod:`sose.organizational.actor_lifecycle`. Stored entity records name
their class ``sose.organizational.od2_actor:OrganizationalActor``, so this module keeps serving
that one class permanently (ADR-0011 point 5). Every other name it used to
re-export was deprecated in 0.11.0 and removed in 0.12.0: import from
``sose.organizational.actor_lifecycle``.
"""
from __future__ import annotations

from sose.organizational.actor_lifecycle import OrganizationalActor

__all__ = ["OrganizationalActor"]

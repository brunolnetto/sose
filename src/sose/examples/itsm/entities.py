from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Incident(Entity):
    entity_type: str = "itsm_incident"


@dataclass(slots=True)
class Escalation(Entity):
    entity_type: str = "itsm_escalation"

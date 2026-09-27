from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Admission(Entity):
    entity_type: str = "hospital_admission"


@dataclass(slots=True)
class TreatmentEpisode(Entity):
    entity_type: str = "hospital_treatment_episode"

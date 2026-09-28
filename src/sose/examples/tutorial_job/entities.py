from dataclasses import dataclass

from sose.api import Entity


@dataclass(slots=True)
class TutorialJob(Entity):
    entity_type: str = "tutorial_job"

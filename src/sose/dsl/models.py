from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RelationshipSpec(BaseModel):
    name: str
    from_entity: str
    to_entity: str
    cardinality: Literal["one-to-one", "one-to-many", "many-to-one", "many-to-many"]


class EntitySpec(BaseModel):
    name: str
    statechart: str
    kind: Literal["master", "stateful", "event"] = "stateful"


class ScenarioSpec(BaseModel):
    name: str
    entity: str
    trigger: Literal["per_tick", "on_event", "scheduled"]
    policy: str
    activation_probability: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Probability that an external scenario/intervention activates; not a StateChart branch probability.",
    )


class DomainSpec(BaseModel):
    name: str
    entities: list[EntitySpec]
    relationships: list[RelationshipSpec] = []
    scenarios: list[ScenarioSpec] = []

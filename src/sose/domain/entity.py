from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Entity:
    id: str
    entity_type: str
    state: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 0

    def touch(self, at: datetime) -> None:
        self.created_at = self.created_at or at
        self.updated_at = at
        self.version += 1

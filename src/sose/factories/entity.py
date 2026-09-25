from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, TypeVar

from sose.core.identity import deterministic_id
from sose.domain.entity import Entity

EntityT = TypeVar("EntityT", bound=Entity)


class EntityFactory:
    """Creates canonical SOSE entities with deterministic identities."""

    def __init__(self, *, now: Callable[[], datetime]) -> None:
        self._now = now

    def create(
        self,
        entity_cls: type[EntityT] = Entity,
        *,
        key: tuple[object, ...],
        entity_type: str | None = None,
        state: str = "",
        attributes: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        **fields: Any,
    ) -> EntityT:
        if not key:
            raise ValueError("entity key must contain at least one part")

        resolved_type = entity_type or getattr(entity_cls, "entity_type", None)
        if not isinstance(resolved_type, str) or not resolved_type:
            try:
                resolved_type = entity_cls.__dataclass_fields__["entity_type"].default
            except Exception as exc:  # pragma: no cover - defensive for custom classes
                raise ValueError("entity_type is required") from exc

        at = created_at or self._now()
        entity_id = deterministic_id("entity", resolved_type, *key)
        entity = entity_cls(
            id=entity_id,
            entity_type=resolved_type,
            state=state,
            attributes=dict(attributes or {}),
            created_at=at,
            updated_at=at,
            **fields,
        )
        return entity

from __future__ import annotations

from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .ledger import (
    ActorCategory,
    ActorLedger,
    ActorLedgerInterval,
    ItemCategory,
    ItemLedger,
    ItemLedgerInterval,
)


_ACTIVE_PRECEDENCE = {
    ItemCategory.REWORK: 0,
    ItemCategory.RECOVERY: 1,
    ItemCategory.COORDINATION: 2,
    ItemCategory.PROCESSING: 3,
}
_WAITING_PRECEDENCE = {
    ItemCategory.WAITING_CALENDAR: 0,
    ItemCategory.WAITING_DECISION: 1,
    ItemCategory.WAITING_INFORMATION: 2,
    ItemCategory.BLOCKED_DEPENDENCY: 3,
    ItemCategory.QUEUE: 4,
    ItemCategory.OTHER_WAIT: 5,
}


class AccountingEvent(BaseModel):
    """Idempotent, validated event projection input for organizational ledgers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: Literal["item", "actor"]
    target_id: str
    start: float
    end: float
    item_category: ItemCategory | None = None
    actor_category: ActorCategory | None = None
    allocation: float = 1.0
    work_item_id: str | None = None
    secondary_labels: tuple[str, ...] = ()
    model_spec_hash: str | None = None

    @model_validator(mode="after")
    def validate_tagged_event(self) -> "AccountingEvent":
        if not isfinite(self.start) or not isfinite(self.end) or self.end <= self.start:
            raise ValueError("accounting event requires finite start < end")
        if not isfinite(self.allocation) or not 0.0 < self.allocation <= 1.0:
            raise ValueError("accounting event allocation must satisfy 0 < allocation <= 1")
        if self.kind == "item":
            if self.item_category is None or self.actor_category is not None:
                raise ValueError("item event requires only item_category")
            if self.work_item_id != self.target_id:
                raise ValueError("item event work_item_id must equal target_id")
        else:
            if self.actor_category is None or self.item_category is not None:
                raise ValueError("actor event requires only actor_category")
        return self

    @classmethod
    def item(
        cls,
        *,
        event_id: str,
        work_item_id: str,
        start: float,
        end: float,
        category: ItemCategory,
        secondary_labels: tuple[str, ...] = (),
        model_spec_hash: str | None = None,
    ) -> "AccountingEvent":
        return cls(
            event_id=event_id,
            kind="item",
            target_id=work_item_id,
            start=start,
            end=end,
            item_category=category,
            work_item_id=work_item_id,
            secondary_labels=secondary_labels,
            model_spec_hash=model_spec_hash,
        )

    @classmethod
    def actor(
        cls,
        *,
        event_id: str,
        actor_id: str,
        start: float,
        end: float,
        category: ActorCategory,
        allocation: float = 1.0,
        work_item_id: str | None = None,
        secondary_labels: tuple[str, ...] = (),
        model_spec_hash: str | None = None,
    ) -> "AccountingEvent":
        return cls(
            event_id=event_id,
            kind="actor",
            target_id=actor_id,
            start=start,
            end=end,
            actor_category=category,
            allocation=allocation,
            work_item_id=work_item_id,
            secondary_labels=secondary_labels,
            model_spec_hash=model_spec_hash,
        )


class LedgerProjector:
    def __init__(self) -> None:
        self._events: dict[str, AccountingEvent] = {}

    def apply(self, event: AccountingEvent) -> None:
        existing = self._events.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ValueError(f"conflicting replay for accounting event {event.event_id!r}")
            return
        self._events[event.event_id] = event

    def item_ledger(self, work_item_id: str) -> ItemLedger:
        events = [
            event
            for event in self._events.values()
            if event.kind == "item" and event.target_id == work_item_id
        ]
        boundaries = sorted({point for event in events for point in (event.start, event.end)})
        intervals: list[ItemLedgerInterval] = []
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            active = [event for event in events if event.start < end and event.end > start]
            if not active:
                continue
            category = resolve_item_primary(*(_require_item_category(event) for event in active))
            hashes = {event.model_spec_hash for event in active if event.model_spec_hash is not None}
            if len(hashes) > 1:
                raise ValueError("overlapping item events span multiple model configurations")
            labels = tuple(sorted({label for event in active for label in event.secondary_labels}))
            intervals.append(
                ItemLedgerInterval(
                    start=start,
                    end=end,
                    primary=category,
                    secondary_labels=labels,
                    model_spec_hash=next(iter(hashes), None),
                )
            )
        return ItemLedger(work_item_id=work_item_id, intervals=intervals)

    def actor_ledger(self, actor_id: str, *, fractional_multitasking: bool = False) -> ActorLedger:
        events = sorted(
            (
                event
                for event in self._events.values()
                if event.kind == "actor" and event.target_id == actor_id
            ),
            key=lambda event: (event.start, event.end, event.event_id),
        )
        intervals = [
            ActorLedgerInterval(
                start=event.start,
                end=event.end,
                category=_require_actor_category(event),
                allocation=event.allocation,
                work_item_id=event.work_item_id,
                secondary_labels=event.secondary_labels,
                model_spec_hash=event.model_spec_hash,
            )
            for event in events
        ]
        return ActorLedger(
            actor_id=actor_id,
            intervals=intervals,
            fractional_multitasking=fractional_multitasking,
        )

    def dump_state(self) -> list[dict[str, object]]:
        return [
            self._events[event_id].model_dump(mode="json")
            for event_id in sorted(self._events)
        ]

    @classmethod
    def from_state(cls, state: list[dict[str, object]]) -> "LedgerProjector":
        projector = cls()
        for payload in state:
            projector.apply(AccountingEvent.model_validate(payload))
        return projector

    def normalized(self) -> list[dict[str, object]]:
        return self.dump_state()


def resolve_item_primary(*categories: ItemCategory) -> ItemCategory:
    if not categories:
        raise ValueError("at least one item category is required")
    active = [category for category in categories if category in _ACTIVE_PRECEDENCE]
    if active:
        return min(active, key=_ACTIVE_PRECEDENCE.__getitem__)
    waiting = [category for category in categories if category in _WAITING_PRECEDENCE]
    if len(waiting) != len(categories):
        raise ValueError("unknown item category")
    return min(waiting, key=_WAITING_PRECEDENCE.__getitem__)


def split_item_interval(
    interval: ItemLedgerInterval,
    *,
    at: float,
    next_model_spec_hash: str,
) -> tuple[ItemLedgerInterval, ItemLedgerInterval]:
    if not interval.start < at < interval.end:
        raise ValueError("split point must be strictly inside the interval")
    shared = {
        "primary": interval.primary,
        "cause": interval.cause,
        "secondary_labels": interval.secondary_labels,
    }
    return (
        ItemLedgerInterval(
            start=interval.start,
            end=at,
            model_spec_hash=interval.model_spec_hash,
            **shared,
        ),
        ItemLedgerInterval(
            start=at,
            end=interval.end,
            model_spec_hash=next_model_spec_hash,
            **shared,
        ),
    )


def _require_item_category(event: AccountingEvent) -> ItemCategory:
    if event.item_category is None:
        raise ValueError(f"item event {event.event_id!r} has no item category")
    return event.item_category


def _require_actor_category(event: AccountingEvent) -> ActorCategory:
    if event.actor_category is None:
        raise ValueError(f"actor event {event.event_id!r} has no actor category")
    return event.actor_category

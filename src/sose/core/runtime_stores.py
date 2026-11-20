"""Durable Store record types, split from ``sose.core.runtime``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StoreDefinition:
    name: str
    kind: str = "fifo"
    capacity: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("store name cannot be empty")
        if self.kind not in {"fifo", "priority", "filter"}:
            raise ValueError("store kind must be one of: fifo, priority, filter")
        if self.capacity is not None and self.capacity < 1:
            raise ValueError("store capacity must be >= 1")


@dataclass(frozen=True, slots=True)
class DurableStoreItem:
    item_id: str
    store_name: str
    value: object
    priority: int
    sequence: int

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("store item sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class StorePutIntent:
    item_id: str
    store_name: str
    value: object
    priority: int
    requested_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("store put sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class StoreGetRequest:
    request_id: str
    store_name: str
    requested_at: datetime
    sequence: int
    filter_key: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("store get sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class StoreGetResult:
    request_id: str
    store_name: str
    item: DurableStoreItem
    completed_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.item.store_name != self.store_name:
            raise ValueError("store get result item belongs to a different store")
        if self.sequence < 1:
            raise ValueError("store get result sequence must be >= 1")



# Defined in ``sose.core.runtime`` before the split and still re-exported from it.
# ``__module__`` stays pinned to that public home so class paths (codec, pickle,
# reprs) are unchanged by the move.
for _moved in (
    StoreDefinition,
    DurableStoreItem,
    StorePutIntent,
    StoreGetRequest,
    StoreGetResult,
):
    _moved.__module__ = "sose.core.runtime"
del _moved

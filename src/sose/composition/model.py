from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Mapping

from sose.core.identity import deterministic_id


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    CONSUMED = "consumed"


def _canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        raise TypeError("boundary payload must be a mapping")
    try:
        encoded = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("boundary payload must be canonical JSON") from exc
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest


@dataclass(frozen=True, slots=True)
class BoundaryMessage:
    message_id: str
    contract_name: str
    contract_version: int
    source_domain: str
    source_identity: str
    destination_domain: str
    occurrence_key: str
    correlation_id: str
    causation_id: str | None
    produced_at: datetime
    payload_json: str
    payload_hash: str

    @classmethod
    def create(
        cls,
        *,
        contract_name: str,
        contract_version: int,
        source_domain: str,
        source_identity: str,
        destination_domain: str,
        occurrence_key: str,
        correlation_id: str,
        causation_id: str | None,
        produced_at: datetime,
        payload: Mapping[str, Any],
    ) -> "BoundaryMessage":
        payload_json, payload_hash = _canonical_payload(payload)
        message_id = deterministic_id(
            "boundary-message",
            source_domain,
            source_identity,
            contract_name,
            contract_version,
            destination_domain,
            occurrence_key,
        )
        return cls(
            message_id=message_id,
            contract_name=contract_name,
            contract_version=contract_version,
            source_domain=source_domain,
            source_identity=source_identity,
            destination_domain=destination_domain,
            occurrence_key=occurrence_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            produced_at=produced_at,
            payload_json=payload_json,
            payload_hash=payload_hash,
        )

    def __post_init__(self) -> None:
        for field_name in (
            "message_id",
            "contract_name",
            "source_domain",
            "source_identity",
            "destination_domain",
            "occurrence_key",
            "correlation_id",
            "payload_json",
            "payload_hash",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} cannot be empty")
        if self.contract_version <= 0:
            raise ValueError("contract_version must be positive")
        try:
            decoded = json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("payload_json must contain valid JSON") from exc
        canonical, digest = _canonical_payload(decoded)
        if canonical != self.payload_json:
            raise ValueError("payload_json must use canonical encoding")
        if digest != self.payload_hash:
            raise ValueError("payload_hash does not match payload_json")

    @property
    def contract_key(self) -> str:
        return f"{self.contract_name}.v{self.contract_version}"

    def payload(self) -> dict[str, Any]:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise TypeError("boundary payload must decode to an object")
        return decoded


@dataclass(frozen=True, slots=True)
class BoundaryDelivery:
    delivery_id: str
    message_id: str
    destination_domain: str
    contract_name: str
    contract_version: int
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    claim_owner_id: str | None = None
    claim_epoch: int = 0
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    consumed_at: datetime | None = None
    consumer_effect_id: str | None = None
    last_error: str | None = None

    @classmethod
    def pending(cls, message: BoundaryMessage) -> "BoundaryDelivery":
        return cls(
            delivery_id=deterministic_id(
                "boundary-delivery",
                message.message_id,
                message.destination_domain,
                message.contract_name,
                message.contract_version,
            ),
            message_id=message.message_id,
            destination_domain=message.destination_domain,
            contract_name=message.contract_name,
            contract_version=message.contract_version,
        )

    def __post_init__(self) -> None:
        if not self.delivery_id or not self.message_id:
            raise ValueError("boundary delivery identity cannot be empty")
        if not self.destination_domain or not self.contract_name:
            raise ValueError("boundary delivery contract cannot be empty")
        if self.contract_version <= 0:
            raise ValueError("contract_version must be positive")
        if self.attempts < 0 or self.claim_epoch < 0:
            raise ValueError("delivery counters must be non-negative")
        if self.status is DeliveryStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.claim_owner_id,
                    self.claimed_at,
                    self.lease_expires_at,
                    self.consumed_at,
                    self.consumer_effect_id,
                )
            ):
                raise ValueError("pending delivery cannot carry claim/consumption state")
        elif self.status is DeliveryStatus.CLAIMED:
            if not self.claim_owner_id or self.claimed_at is None or self.lease_expires_at is None:
                raise ValueError("claimed delivery requires owner and lease")
            if self.claim_epoch <= 0:
                raise ValueError("claimed delivery requires positive fencing epoch")
            if self.consumed_at is not None or self.consumer_effect_id is not None:
                raise ValueError("claimed delivery cannot be consumed")
        elif self.status is DeliveryStatus.CONSUMED:
            if self.consumed_at is None or not self.consumer_effect_id:
                raise ValueError("consumed delivery requires effect and timestamp")


@dataclass(frozen=True, slots=True)
class BoundaryLease:
    delivery_id: str
    message_id: str
    owner_id: str
    epoch: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.delivery_id or not self.message_id or not self.owner_id:
            raise ValueError("boundary lease identity cannot be empty")
        if self.epoch <= 0:
            raise ValueError("boundary lease epoch must be positive")


@dataclass(frozen=True, slots=True)
class BoundaryConsumption:
    consumption_id: str
    delivery_id: str
    message_id: str
    destination_domain: str
    contract_name: str
    contract_version: int
    consumer_effect_id: str
    consumed_at: datetime

    @classmethod
    def create(
        cls,
        *,
        delivery: BoundaryDelivery,
        consumer_effect_id: str,
        consumed_at: datetime,
    ) -> "BoundaryConsumption":
        return cls(
            consumption_id=deterministic_id(
                "boundary-consumption",
                delivery.delivery_id,
                delivery.message_id,
                delivery.destination_domain,
                delivery.contract_name,
                delivery.contract_version,
            ),
            delivery_id=delivery.delivery_id,
            message_id=delivery.message_id,
            destination_domain=delivery.destination_domain,
            contract_name=delivery.contract_name,
            contract_version=delivery.contract_version,
            consumer_effect_id=consumer_effect_id,
            consumed_at=consumed_at,
        )

    def __post_init__(self) -> None:
        if not self.consumption_id or not self.delivery_id or not self.message_id:
            raise ValueError("boundary consumption identity cannot be empty")
        if not self.consumer_effect_id:
            raise ValueError("consumer_effect_id cannot be empty")

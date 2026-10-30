from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Mapping

from sose.core.identity import deterministic_id


class DeliveryStatus(Enum):
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


_ORGANIZATION_FINGERPRINT = re.compile(r"[0-9a-f]{64}\\Z")


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
    causation_kind: str | None = None  # None preserves opaque frozen v1 IDs.
    # Optional v2 organizational route binding. All fields are absent on
    # historical/domain-scoped v1 messages; partial bindings fail closed.
    organization_id: str | None = None
    definition_fingerprint: str | None = None
    config_epoch: int | None = None
    source_component_id: str | None = None
    source_owner_id: str | None = None
    destination_component_id: str | None = None
    destination_owner_id: str | None = None

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
        causation_kind: str | None = None,
        payload: Mapping[str, Any],
        organization_id: str | None = None,
        definition_fingerprint: str | None = None,
        config_epoch: int | None = None,
        source_component_id: str | None = None,
        source_owner_id: str | None = None,
        destination_component_id: str | None = None,
        destination_owner_id: str | None = None,
    ) -> "BoundaryMessage":
        payload_json, payload_hash = _canonical_payload(payload)
        organizational_binding = (
            organization_id,
            definition_fingerprint,
            config_epoch,
            source_component_id,
            source_owner_id,
            destination_component_id,
            destination_owner_id,
        )
        if all(value is None for value in organizational_binding):
            # Preserve the exact historical v1 identity derivation.
            message_id = deterministic_id(
                "boundary-message",
                source_domain,
                source_identity,
                contract_name,
                contract_version,
                destination_domain,
                occurrence_key,
            )
        else:
            if any(value is None for value in organizational_binding):
                raise ValueError("organizational boundary binding must be complete")
            message_id = deterministic_id(
                "boundary-message-organization-v2",
                source_domain,
                source_identity,
                contract_name,
                contract_version,
                destination_domain,
                occurrence_key,
                *organizational_binding,
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
            causation_kind=causation_kind,
            payload_json=payload_json,
            payload_hash=payload_hash,
            organization_id=organization_id,
            definition_fingerprint=definition_fingerprint,
            config_epoch=config_epoch,
            source_component_id=source_component_id,
            source_owner_id=source_owner_id,
            destination_component_id=destination_component_id,
            destination_owner_id=destination_owner_id,
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
        if self.causation_kind not in (None, "boundary", "event"):
            raise ValueError("unsupported causal reference kind")
        if self.causation_kind is not None and not self.causation_id:
            raise ValueError("typed causation requires a nonempty causation_id")

        organizational_binding = (
            self.organization_id,
            self.definition_fingerprint,
            self.config_epoch,
            self.source_component_id,
            self.source_owner_id,
            self.destination_component_id,
            self.destination_owner_id,
        )
        if any(value is not None for value in organizational_binding):
            if any(value is None for value in organizational_binding):
                raise ValueError("organizational boundary binding must be complete")
            for label in (
                "organization_id",
                "source_component_id",
                "source_owner_id",
                "destination_component_id",
                "destination_owner_id",
            ):
                value = getattr(self, label)
                if not isinstance(value, str) or not value.strip() or value != value.strip():
                    raise ValueError(f"{label} must be a canonical nonempty identity")
            if (
                type(self.config_epoch) is not int
                or self.config_epoch < 0
            ):
                raise ValueError("config_epoch must be a nonnegative integer")
            if not isinstance(self.definition_fingerprint, str) or not _ORGANIZATION_FINGERPRINT.fullmatch(
                self.definition_fingerprint
            ):
                raise ValueError("definition_fingerprint must be a lowercase SHA-256")
            expected_id = deterministic_id(
                "boundary-message-organization-v2",
                self.source_domain,
                self.source_identity,
                self.contract_name,
                self.contract_version,
                self.destination_domain,
                self.occurrence_key,
                *organizational_binding,
            )
            if self.message_id != expected_id:
                raise ValueError("organizational boundary message identity contradicts binding")

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
    def organization_scoped(self) -> bool:
        return self.organization_id is not None

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

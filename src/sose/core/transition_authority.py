"""Opt-in Engine-issued transition proofs with an externally held signing root.

A writable UnitOfWork can append a syntactically perfect StateChart event.
Only an Engine explicitly configured with a separately provisioned secret may
attest actual execution. The key must NOT be persisted in a DomainEvent,
UnitOfWork or the simulation dataset. This does not itself provide DB roles.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from json import dumps

from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity


ATTESTATION_EVENT = "engine.statechart_transition_attested"


def _encoded(data: dict) -> bytes:
    return dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _utc_timestamp(value: datetime) -> datetime:
    """Reject timezone-free or untyped timestamps before signing evidence."""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("Engine attestation requires timezone-aware causal timestamps")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True, repr=False)
class TransitionAuthority:
    """A capability issued OUTSIDE public UoW writer access.

    This local reference implementation requires deployment-level key/role
    separation. Code running in the same trusted Python process as the Engine
    is not a cryptographically isolated adversary.
    """

    issuer_id: str
    key_id: str
    secret: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise ValueError("transition authority requires a protected 256-bit secret")
        if not all(isinstance(x, str) and x.strip() for x in (self.issuer_id, self.key_id)):
            raise ValueError("issuer and key IDs must be nonblank")

    def _record_id(self, event_id: str) -> str:
        return deterministic_id("engine-attested-transition", event_id)

    def seal(self, *, before: Entity, after: Entity, event: DomainEvent,
             command: Command) -> DomainEvent:
        """Seal an actual emitted StateChart transition inside Engine's UoW."""
        # An attestation cannot authorize an event that predates the command,
        # its due time or its predecessor's last durable update. In particular,
        # never sign a forged event for a different Command target.
        event_at = _utc_timestamp(event.occurred_at)
        due_at = _utc_timestamp(command.due_at)
        before_at = _utc_timestamp(before.updated_at)
        _utc_timestamp(after.updated_at)
        issued_at = (
            _utc_timestamp(command.issued_at)
            if command.issued_at is not None else None
        )
        payload = event.payload
        if (
            event_at < due_at
            or event_at < before_at
            or (issued_at is not None and event_at < issued_at)
            or command.entity_id != after.id
            or command.entity_type != after.entity_type
            or event.name != "entity.state_transition"
            or event.owner_id != command.owner_id
            or event.causation_id != command.command_id
            or event.entity_id != after.id or before.id != after.id
            or event.entity_type != after.entity_type or before.entity_type != after.entity_type
            or event.payload.get("trigger") != command.name
            or payload.get("from_state") != before.state
            or payload.get("to_state") != after.state
            or type(event.tick) is not int
            or type(before.version) is not int
            or type(after.version) is not int
            or after.version != before.version + 1
            or after.updated_at != event.occurred_at
        ):
            raise ValueError("Engine attestation requires one genuine committed transition")
        expected_id = deterministic_id(
            "event", event.owner_id, event.tick, after.entity_type, after.id,
            after.version, command.name, before.state, after.state,
        )
        if (
            event.event_id != expected_id
            or event.correlation_id != (command.correlation_id or command.command_id)
            or set(event.payload) != {"trigger", "from_state", "to_state"}
        ):
            raise ValueError("Engine attestation refuses unrecorded source-event identity")
        claims = {
            "event_name": event.name,
            "event_payload_sha256": sha256(_encoded(dict(event.payload))).hexdigest(),
            "issuer_id": self.issuer_id, "key_id": self.key_id,
            "event_id": event.event_id, "owner_id": event.owner_id,
            "command_id": command.command_id,
            "entity_type": after.entity_type, "entity_id": after.id,
            "from_state": before.state, "to_state": after.state,
            "from_version": before.version, "to_version": after.version,
            "trigger": command.name, "occurred_at": event.occurred_at.isoformat(),
            "tick": event.tick, "correlation_id": event.correlation_id,
        }
        signature = hmac_new(self.secret, _encoded(claims), sha256).hexdigest()
        return DomainEvent(
            event_id=self._record_id(event.event_id),
            name=ATTESTATION_EVENT, entity_type=event.entity_type,
            entity_id=event.entity_id, occurred_at=event.occurred_at,
            tick=event.tick, owner_id=event.owner_id,
            causation_id=event.event_id, correlation_id=event.correlation_id,
            payload={**claims, "signature": signature},
        )

    def validate_prestate(
        self, *, uow, before: Entity, chart, owner_id: str,
    ) -> None:
        """Require a complete issuer-authenticated transition chain before send.

        Checking just the latest signed transition is insufficient: an older
        unauthorized UoW write might skip versions 1-2 and obtain a genuine
        signature over the later version 3. This guard is opt-in for Engine;
        deployment key isolation and PostgreSQL owner fencing remain separate.
        """
        if type(before.version) is not int or before.version < 0:
            raise ValueError("unauthenticated prestate: invalid entity version")
        initial = {
            str(state.id)
            for state in chart.states
            if getattr(state, "initial", False)
        }
        if len(initial) != 1:
            raise ValueError("unauthenticated prestate: ambiguous chart initial state")

        history = tuple(
            event for event in uow.events()
            if event.entity_type == before.entity_type
            and event.entity_id == before.id
            and event.name == "entity.state_transition"
        )
        if before.version == 0:
            if before.state not in initial or history:
                raise ValueError(
                    "unauthenticated prestate: version-zero entity must have "
                    "its initial chart state and no transition history"
                )
            return

        if len(history) != before.version:
            raise ValueError(
                "trusted predecessor history incomplete or duplicated"
            )

        authenticated = {}
        for event in history:
            if event.owner_id != owner_id:
                raise ValueError("trusted predecessor has foreign owner")
            receipt = self.verify(uow=uow, event=event)
            version = receipt.payload["to_version"]
            if version in authenticated:
                raise ValueError("trusted predecessor version replayed")
            authenticated[version] = (event, receipt.payload)

        prior_state = next(iter(initial))
        prior_time = None
        for version in range(1, before.version + 1):
            if version not in authenticated:
                raise ValueError("trusted predecessor chain has missing version")
            event, claims = authenticated[version]
            if (
                claims["from_version"] != version - 1
                or claims["from_state"] != prior_state
                or event.owner_id != owner_id
                or (prior_time is not None and event.occurred_at < prior_time)
            ):
                raise ValueError(
                    "trusted predecessor chain has invalid order or owner"
                )
            prior_state = claims["to_state"]
            prior_time = event.occurred_at

        if (
            prior_state != before.state
            or before.updated_at != prior_time
        ):
            raise ValueError(
                "trusted predecessor chain contradicts committed projection"
            )

    def verify(self, *, uow, event: DomainEvent) -> DomainEvent:
        """Verify bound issuer, source event and MAC; fail closed on replay."""
        source_matches = [x for x in uow.events() if x.event_id == event.event_id]
        if len(source_matches) != 1 or source_matches[0] != event:
            raise ValueError("authenticated StateChart source event missing, duplicated or modified")
        receipt_id = self._record_id(event.event_id)
        matches = [x for x in uow.events() if x.event_id == receipt_id]
        if len(matches) != 1:
            raise ValueError("authenticated StateChart receipt missing or duplicated")
        receipt = matches[0]
        if (type(event.tick) is not int or type(receipt.tick) is not int
            or receipt.name != ATTESTATION_EVENT or receipt.causation_id != event.event_id
            or receipt.owner_id != event.owner_id
            or receipt.entity_type != event.entity_type or receipt.entity_id != event.entity_id
            or receipt.occurred_at != event.occurred_at
            or receipt.tick != event.tick
            or receipt.correlation_id != event.correlation_id):
            raise ValueError("authenticated StateChart receipt envelope mismatch")
        data = dict(receipt.payload)
        mac = data.pop("signature", None)
        if not isinstance(mac, str) or len(mac) != 64:
            raise ValueError("authenticated StateChart receipt signature missing")
        if data.get("issuer_id") != self.issuer_id or data.get("key_id") != self.key_id:
            raise ValueError("authenticated StateChart receipt issuer mismatch")
        expected = hmac_new(self.secret, _encoded(data), sha256).hexdigest()
        if not compare_digest(mac, expected):
            raise ValueError("authenticated StateChart receipt signature invalid")
        if (
            event.name != "entity.state_transition"
            or data.get("event_name") != event.name
            or data.get("event_payload_sha256") != sha256(
                _encoded(dict(event.payload)),
            ).hexdigest()
            or data.get("event_id") != event.event_id
            or data.get("owner_id") != event.owner_id
            or data.get("entity_id") != event.entity_id
            or data.get("entity_type") != event.entity_type
            or data.get("occurred_at") != event.occurred_at.isoformat()
            or type(data.get("tick")) is not int
            or data.get("tick") != event.tick
            or data.get("correlation_id") != event.correlation_id
            or data.get("trigger") != event.payload.get("trigger")
            or data.get("from_state") != event.payload.get("from_state")
            or data.get("to_state") != event.payload.get("to_state")
            or data.get("command_id") != event.causation_id
            or type(data.get("from_version")) is not int
            or type(data.get("to_version")) is not int
            or data["to_version"] != data["from_version"] + 1
        ):
            raise ValueError("authenticated StateChart receipt evidence contradicts event")
        current = uow.get_entity(event.entity_type, event.entity_id)
        if (
            current is None
            or type(current.version) is not int
            or current.version < data["to_version"]
        ):
            raise ValueError("authenticated StateChart receipt lacks committed entity version")
        if current.version == data["to_version"] and current.state != data["to_state"]:
            raise ValueError("authenticated StateChart receipt contradicts terminal projection")
        return receipt

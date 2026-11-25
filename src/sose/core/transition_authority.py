"""Opt-in Engine-issued transition proofs with an externally held signing root.

A writable UnitOfWork can append a syntactically perfect StateChart event.
Only an Engine explicitly configured with a separately provisioned secret may
attest actual execution. The key must NOT be persisted in a DomainEvent,
UnitOfWork or the simulation dataset. This does not itself provide DB roles.

Receipts form a per-entity linked chain with one receipt id per entity
version (independent of owner): each signs its predecessor receipt id and a
digest of the resulting entity attributes, so Engine authenticates a
predecessor with a constant number of event-id lookups and no log
enumeration or copy (lookup cost itself is adapter-specific). Enabling an authority
on entities that already have unsigned (version > 0) history fails closed;
there is no re-anchoring procedure for such entities.
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
from sose.persistence.codec import dumps as codec_dumps


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

    def seal(self, *, before: Entity, after: Entity, event: DomainEvent,
             command: Command, predecessor: DomainEvent | None = None) -> DomainEvent:
        """Seal an actual emitted StateChart transition inside Engine's UoW.

        ``predecessor`` is the receipt returned by :meth:`validate_prestate`.
        Its id is signed into the new receipt, chaining every version to the
        previous one; a receipt sealed without it anchors only version 1.
        The claims also bind a digest of ``after.attributes`` so a forged
        attribute projection cannot pass as the signed terminal state.
        """
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
            "attributes_sha256": _attributes_sha256(after),
            "predecessor_receipt_id": _predecessor_claim(before, event, predecessor),
        }
        signature = hmac_new(self.secret, _encoded(claims), sha256).hexdigest()
        return DomainEvent(
            event_id=_receipt_id(after.entity_type, after.id, after.version),
            name=ATTESTATION_EVENT, entity_type=event.entity_type,
            entity_id=event.entity_id, occurred_at=event.occurred_at,
            tick=event.tick, owner_id=event.owner_id,
            causation_id=event.event_id, correlation_id=event.correlation_id,
            payload={**claims, "signature": signature},
        )

    def validate_prestate(
        self, *, uow, before: Entity, chart, owner_id: str,
    ) -> DomainEvent | None:
        """Authenticate the complete StateChart predecessor before ``send``.

        Returns the latest receipt for ``before`` (``None`` at version zero);
        Engine passes it to :meth:`seal`, which signs its id into the new
        receipt. Every sealed receipt therefore names its predecessor, and is
        only issued after that predecessor was itself validated here. So the
        check needs a constant number of ``get_event`` id lookups (the receipt
        for the current version, the absent one for the next version, and the
        head's source event) and no log enumeration or copy via
        ``uow.events()``. Lookup cost is adapter-specific: the shared
        in-memory UoW still scans its working log inside ``get_event``.

        A genuinely signed latest receipt alone is insufficient: an older
        unauthorized UoW write might skip versions 1-2 and still obtain a
        signature over version 3. Such a receipt has no predecessor link and
        is rejected. Receipt ids are per entity version and owner-independent:
        a version already claimed by ANY owner's receipt blocks every owner
        (no fork after a rewind), while only a head receipt sealed for
        ``owner_id`` can anchor this owner's next transition. Another owner's
        plain ``entity.state_transition`` events are ignored.

        Accepted fail-closed denial of service: a public writer that appends
        any event under the next receipt id, or a conflicting event under an
        existing one, blocks attested dispatch for that entity. Nothing is
        written in that case; integrity is preserved, availability is not.

        Enabling the authority on an entity with existing unsigned history
        fails closed: a version above zero without this owner's anchored
        receipt chain is rejected and there is no re-anchoring procedure.
        This guard is opt-in for Engine; deployment key isolation and
        PostgreSQL owner fencing remain separate.
        """
        if type(before.version) is not int or before.version < 0:
            raise ValueError("unauthenticated prestate: invalid entity version")
        initial = _initial_states(chart)
        successor = uow.get_event(_receipt_id(
            before.entity_type, before.id, before.version + 1,
        ))
        if before.version == 0:
            if before.state not in initial or successor is not None:
                raise ValueError(
                    "unauthenticated prestate: version-zero entity must have "
                    "its initial chart state and no transition history"
                )
            return None
        if successor is not None:
            raise ValueError("trusted predecessor version replayed")
        head = uow.get_event(_receipt_id(before.entity_type, before.id, before.version))
        if head is None:
            raise ValueError("trusted predecessor history incomplete")
        source = uow.get_event(head.causation_id)
        if source is None:
            raise ValueError("authenticated StateChart source event missing")
        claims = self._authenticate(receipt=head, event=source)
        _require_anchored(claims, before, initial, owner_id)
        _require_projection(claims, before, source)
        return head

    def verify(self, *, uow, event: DomainEvent) -> DomainEvent:
        """Verify bound issuer, source event and MAC; fail closed on replay.

        This is an audit API: it scans the UoW event log once to reject
        duplicated source events or receipts. Engine's dispatch path uses
        :meth:`validate_prestate` instead, which does no log scan.
        """
        log = uow.events()
        source_matches = [x for x in log if x.event_id == event.event_id]
        if len(source_matches) != 1 or source_matches[0] != event:
            raise ValueError(
                "authenticated StateChart source event missing, duplicated or modified"
            )
        matches = [
            x for x in log
            if x.name == ATTESTATION_EVENT and x.causation_id == event.event_id
        ]
        if len(matches) != 1:
            raise ValueError("authenticated StateChart receipt missing or duplicated")
        receipt = matches[0]
        data = self._authenticate(receipt=receipt, event=event)
        current = uow.get_entity(event.entity_type, event.entity_id)
        if (
            current is None
            or type(current.version) is not int
            or current.version < data["to_version"]
        ):
            raise ValueError("authenticated StateChart receipt lacks committed entity version")
        if current.version == data["to_version"] and (
            current.state != data["to_state"]
            or _attributes_sha256(current) != data["attributes_sha256"]
        ):
            raise ValueError("authenticated StateChart receipt contradicts terminal projection")
        return receipt

    def verify_history(
        self, *, transitions, receipts_by_source: dict, entity: Entity,
    ) -> DomainEvent:
        """Authenticate one entity's whole transition history; return the head receipt.

        ``transitions`` are the entity's ``entity.state_transition`` events in commit
        order, and ``receipts_by_source`` maps a source event id to its receipt. Each
        transition needs its own receipt, each receipt must name the previous one
        (the first names none), and the last must describe the stored ``entity``:
        version, state and attribute digest. A forged, unsigned, reordered or
        missing step, or a projection rewritten after the last receipt, fails closed.
        """
        if not transitions:
            raise ValueError("no StateChart transition to authenticate")
        previous_id = None
        for event in transitions:
            receipt = receipts_by_source.get(event.event_id)
            if receipt is None:
                raise ValueError("unattested StateChart transition: receipt missing")
            claims = self._authenticate(receipt=receipt, event=event)
            if claims.get("predecessor_receipt_id") != previous_id:
                raise ValueError("authenticated StateChart receipt chain is broken")
            if previous_id is None and claims.get("from_version") != 0:
                raise ValueError("authenticated StateChart chain does not start at version zero")
            previous_id = receipt.event_id
        _require_stored_projection(claims, entity, event)
        return receipt

    def _authenticate(self, *, receipt: DomainEvent, event: DomainEvent) -> dict:
        """Check receipt envelope, issuer MAC and signed evidence for ``event``."""
        if _envelope_mismatch(receipt, event):
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
        if _evidence_mismatch(data, event) or receipt.event_id != _receipt_id(
            event.entity_type, event.entity_id, data["to_version"],
        ):
            raise ValueError("authenticated StateChart receipt evidence contradicts event")
        return data


def _require_stored_projection(claims: dict, entity: Entity, source: DomainEvent) -> None:
    """The stored entity is exactly the last receipt's projection (as before a dispatch)."""
    if (
        claims["to_version"] != entity.version or claims["to_state"] != entity.state
        or claims["attributes_sha256"] != _attributes_sha256(entity)
        or entity.updated_at != source.occurred_at
    ):
        raise ValueError("authenticated StateChart receipt contradicts the stored entity")


def _receipt_id(entity_type: str, entity_id: str, version: int) -> str:
    """Owner-independent receipt identity: one receipt per entity version.

    Entities are not owner-scoped, so a version claimed by any owner's receipt
    is claimed for every owner; this prevents a fork after a projection rewind.
    """
    return deterministic_id("engine-attested-transition", entity_type, entity_id, version)


def _attributes_sha256(entity: Entity) -> str:
    """Digest of the typed durable attribute encoding (fails on unsupported values)."""
    return sha256(codec_dumps(entity.attributes).encode("utf-8")).hexdigest()


def _initial_states(chart) -> set[str]:
    initial = {str(state.id) for state in chart.states if getattr(state, "initial", False)}
    if len(initial) != 1:
        raise ValueError("unauthenticated prestate: ambiguous chart initial state")
    return initial


def _predecessor_claim(before: Entity, event: DomainEvent,
                       predecessor: DomainEvent | None) -> str | None:
    """Return the predecessor receipt id to sign, refusing a mismatched link."""
    if predecessor is None:
        return None
    expected = _receipt_id(before.entity_type, before.id, before.version)
    if (
        predecessor.event_id != expected
        or predecessor.payload.get("to_version") != before.version
    ):
        raise ValueError("Engine attestation predecessor receipt does not match prestate")
    return expected


def _envelope_mismatch(receipt: DomainEvent, event: DomainEvent) -> bool:
    pairs = (
        (receipt.name, ATTESTATION_EVENT), (receipt.causation_id, event.event_id),
        (receipt.owner_id, event.owner_id), (receipt.entity_type, event.entity_type),
        (receipt.entity_id, event.entity_id), (receipt.occurred_at, event.occurred_at),
        (receipt.tick, event.tick), (receipt.correlation_id, event.correlation_id),
    )
    return (
        type(event.tick) is not int or type(receipt.tick) is not int
        or any(left != right for left, right in pairs)
    )


def _evidence_mismatch(data: dict, event: DomainEvent) -> bool:
    expected = {
        "event_name": event.name,
        "event_payload_sha256": sha256(_encoded(dict(event.payload))).hexdigest(),
        "event_id": event.event_id, "owner_id": event.owner_id,
        "entity_id": event.entity_id, "entity_type": event.entity_type,
        "occurred_at": event.occurred_at.isoformat(), "tick": event.tick,
        "correlation_id": event.correlation_id,
        "trigger": event.payload.get("trigger"),
        "from_state": event.payload.get("from_state"),
        "to_state": event.payload.get("to_state"),
        "command_id": event.causation_id,
    }
    typed = (data.get("tick"), data.get("from_version"), data.get("to_version"))
    return (
        event.name != "entity.state_transition"
        or any(data.get(key) != value for key, value in expected.items())
        or any(type(value) is not int for value in typed)
        or data["to_version"] != data["from_version"] + 1
        or not isinstance(data.get("attributes_sha256"), str)
    )


def _require_anchored(claims: dict, before: Entity, initial: set[str], owner_id: str) -> None:
    """The head receipt must link to the previous version, or start at the chart root."""
    if claims["owner_id"] != owner_id:
        raise ValueError("trusted predecessor has foreign owner")
    version = before.version
    expected = None if version == 1 else _receipt_id(before.entity_type, before.id, version - 1)
    if claims.get("predecessor_receipt_id") != expected:
        raise ValueError("trusted predecessor chain has missing version")
    if version == 1 and claims["from_state"] not in initial:
        raise ValueError("trusted predecessor chain does not start at the initial state")


def _require_projection(claims: dict, before: Entity, source: DomainEvent) -> None:
    """The stored entity must be exactly the signed terminal projection."""
    if (
        claims["to_version"] != before.version
        or claims["to_state"] != before.state
        or claims["attributes_sha256"] != _attributes_sha256(before)
    ):
        raise ValueError("authenticated StateChart receipt contradicts terminal projection")
    if before.updated_at != source.occurred_at:
        raise ValueError("trusted predecessor chain contradicts committed projection")

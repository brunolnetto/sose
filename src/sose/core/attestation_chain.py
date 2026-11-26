"""Attested attribute-only saves, chained with transition receipts (#494, #545).

A transition receipt (``transition_authority``) signs one StateChart transition and
the attribute digest after it. Domain code may also save an entity's attributes
outside a transition, keeping its version and state. Under an authority, each such
save is attested here:

- the entity's ``attribute_revision`` goes up by one (a transition resets it);
- a receipt ``engine.entity_attributes_attested`` is issued in the same transaction,
  with id ``(entity_type, entity_id, version, revision)``. It signs the state, the
  new attribute digest and ``updated_at``, and names the previous receipt of the
  entity (a transition receipt or another attribute receipt);
- a save that changes the state without a transition is refused.

The head of an entity's chain is found from the stored entity in constant time:
revision 0 is the transition receipt of its version (none at version 0), and
revision ``r > 0`` is the attribute receipt ``(version, r)``. A receipt already
present for the next revision, or for the next version, means the stored entity
was rolled back, and the next attested step fails closed.

``AttributeAttestingPersistence`` wraps the persistence a job hands its domain, so
every attribute-only save made through it is attested without changing domain code.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity

ATTRIBUTES_EVENT = "engine.entity_attributes_attested"


def attribute_receipt_id(entity_type: str, entity_id: str, version: int, revision: int) -> str:
    return deterministic_id(
        "engine-attested-attributes", entity_type, entity_id, version, revision,
    )


def head_receipt_id(entity: Entity) -> str | None:
    """The id of the receipt the stored ``entity`` must be the projection of."""
    from sose.core.transition_authority import _receipt_id

    if entity.attribute_revision > 0:
        return attribute_receipt_id(
            entity.entity_type, entity.id, entity.version, entity.attribute_revision,
        )
    return None if entity.version == 0 else _receipt_id(entity.entity_type, entity.id, entity.version)


def require_no_successor(uow, entity: Entity) -> None:
    """A receipt beyond the stored entity's position means it was rolled back."""
    from sose.core.transition_authority import _receipt_id

    for successor in (
        _receipt_id(entity.entity_type, entity.id, entity.version + 1),
        attribute_receipt_id(
            entity.entity_type, entity.id, entity.version, entity.attribute_revision + 1,
        ),
    ):
        if uow.get_event(successor) is not None:
            raise ValueError("trusted predecessor version replayed")


def _timestamp(value) -> str | None:
    return None if value is None else value.isoformat()


def seal_attributes(authority, *, before: Entity, after: Entity, owner_id: str,
                    predecessor: DomainEvent | None) -> DomainEvent:
    """Sign one attribute-only save of ``before`` into ``after``."""
    from sose.core.transition_authority import _attributes_sha256

    if (
        after.version != before.version or after.state != before.state
        or after.attribute_revision != before.attribute_revision + 1
    ):
        raise ValueError("an attested attribute save keeps version and state")
    expected = head_receipt_id(before)
    if (None if predecessor is None else predecessor.event_id) != expected:
        raise ValueError("attribute attestation predecessor does not match the stored entity")
    claims = {
        "kind": "attributes", "issuer_id": authority.issuer_id, "key_id": authority.key_id,
        "owner_id": owner_id, "entity_type": after.entity_type, "entity_id": after.id,
        "version": after.version, "from_revision": before.attribute_revision,
        "to_revision": after.attribute_revision, "state": after.state,
        "attributes_sha256": _attributes_sha256(after),
        "updated_at": _timestamp(after.updated_at), "predecessor_receipt_id": expected,
    }
    return DomainEvent(
        event_id=attribute_receipt_id(after.entity_type, after.id, after.version,
                                      after.attribute_revision),
        name=ATTRIBUTES_EVENT, entity_type=after.entity_type, entity_id=after.id,
        occurred_at=after.updated_at or after.created_at, owner_id=owner_id,
        causation_id=expected, payload=authority._signed(claims),
    )


def authenticate_attributes(authority, receipt: DomainEvent) -> dict:
    """Check an attribute receipt's envelope, issuer and MAC; return its claims."""
    data = dict(receipt.payload)
    if receipt.name != ATTRIBUTES_EVENT or data.get("kind") != "attributes":
        raise ValueError("attribute receipt envelope mismatch")
    authority._require_signed(data, "attribute receipt")
    _require_attribute_envelope(receipt, data)
    return data


def _require_attribute_envelope(receipt: DomainEvent, data: dict) -> None:
    if (
        receipt.event_id != attribute_receipt_id(
            data["entity_type"], data["entity_id"], data["version"], data["to_revision"])
        or (receipt.entity_type, receipt.entity_id, receipt.owner_id)
        != (data["entity_type"], data["entity_id"], data["owner_id"])
        or data["to_revision"] != data["from_revision"] + 1
    ):
        raise ValueError("attribute receipt evidence contradicts its envelope")


def require_attribute_projection(claims: dict, entity: Entity, owner_id: str | None) -> None:
    """The stored entity is exactly what the attribute receipt signed."""
    from sose.core.transition_authority import _attributes_sha256

    if owner_id is not None and claims["owner_id"] != owner_id:
        raise ValueError("trusted predecessor has foreign owner")
    if (
        claims["version"] != entity.version or claims["to_revision"] != entity.attribute_revision
        or claims["state"] != entity.state
        or claims["attributes_sha256"] != _attributes_sha256(entity)
        or claims["updated_at"] != _timestamp(entity.updated_at)
    ):
        raise ValueError("authenticated attribute receipt contradicts the stored entity")


def expected_attribute_predecessor(claims: dict) -> str | None:
    """The receipt an attribute receipt must name: the head before its revision."""
    from sose.core.transition_authority import _receipt_id

    if claims["from_revision"] > 0:
        return attribute_receipt_id(claims["entity_type"], claims["entity_id"],
                                    claims["version"], claims["from_revision"])
    if claims["version"] == 0:
        return None
    return _receipt_id(claims["entity_type"], claims["entity_id"], claims["version"])


class AttributeAttestingPersistence:
    """Persistence whose units of work attest every attribute-only entity save.

    ``registry`` is the domain's ``DomainRegistry`` (the job binds it each tick). An
    entity whose type has a StateChart in that registry starts its chain only from the
    chart's initial state: signing a version-zero entity in any other state would turn
    an unsigned state write into an authenticated chain. A type absent from the
    registry (for example one only another component registers) is treated as
    chart-less. Until a registry is bound (during ``initialize``/seed, and while the
    first tick's runtime is being built), a save of an
    entity still at version 0, revision 0 is stored unsigned, like a creation: nothing
    is signed, so nothing is laundered, and the first attested step checks the root.
    """

    def __init__(self, inner, *, authority, owner_id: str, registry=None) -> None:
        self._inner = inner
        self._authority = authority
        self._owner_id = owner_id
        self.registry = registry

    @contextmanager
    def transaction(self, *args, **kwargs):
        with self._inner.transaction(*args, **kwargs) as uow:
            yield _AttestingUnitOfWork(uow, self._authority, self._owner_id, self.registry)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _AttestingUnitOfWork:
    def __init__(self, uow, authority, owner_id: str, registry) -> None:
        self._uow = uow
        self._authority = authority
        self._owner_id = owner_id
        self._registry = registry

    def save_entity(self, entity: Entity) -> None:
        existing = self._uow.get_entity(entity.entity_type, entity.id)
        if existing is None or existing.version != entity.version:
            # A creation, or an Engine transition, which the Engine itself attests.
            return self._uow.save_entity(entity)
        if existing.state != entity.state:
            raise ValueError("a state change needs an attested StateChart transition")
        if self._unsigned_save(existing, entity):
            return self._uow.save_entity(entity)
        predecessor = self._authority.validate_prestate(
            uow=self._uow, before=existing, chart=self._root_chart(existing),
            owner_id=self._owner_id,
        )
        after = deepcopy(entity)
        after.attribute_revision = existing.attribute_revision + 1
        receipt = seal_attributes(self._authority, before=existing, after=after,
                                  owner_id=self._owner_id, predecessor=predecessor)
        self._uow.save_entity(after)
        self._uow.append_event(receipt)
        # The caller keeps using its object: keep its revision in step with the store.
        entity.attribute_revision = after.attribute_revision

    def _unsigned_save(self, existing: Entity, entity: Entity) -> bool:
        """Nothing to attest, or an unsigned root saved before a registry is bound."""
        if (
            existing.attributes == entity.attributes and existing.updated_at == entity.updated_at
            and existing.attribute_revision == entity.attribute_revision
        ):
            return True
        return (
            self._registry is None and existing.version == 0
            and existing.attribute_revision == 0 == entity.attribute_revision
        )

    def _root_chart(self, existing: Entity):
        """The chart that pins the initial state when a chain starts at version 0."""
        if existing.version != 0 or existing.attribute_revision != 0:
            return None
        if self._registry is None:
            raise ValueError("an attested first attribute save needs the domain registry")
        if not self._registry.supports(existing.entity_type):
            return None                   # no StateChart: the state is not chart-controlled
        return self._registry.chart_for(deepcopy(existing))

    def __getattr__(self, name):
        return getattr(self._uow, name)


def attribute_head(authority, uow, before: Entity, owner_id: str) -> DomainEvent:
    """Authenticate the attribute receipt at the head of ``before``'s chain."""
    head = uow.get_event(head_receipt_id(before))
    if head is None:
        raise ValueError("trusted predecessor history incomplete")
    claims = authenticate_attributes(authority, head)
    if claims["predecessor_receipt_id"] != expected_attribute_predecessor(claims):
        raise ValueError("trusted predecessor chain has missing version")
    require_attribute_projection(claims, before, owner_id)
    return head


def verify_entity_history(authority, *, events, entity: Entity) -> DomainEvent:
    """Walk one entity's receipts in commit order; the last must be the stored entity.

    Every transition needs exactly one receipt; transition and attribute receipts
    each name the previous one (the very first names none and starts at version
    zero, revision zero), and all are issued for one owner. A forged, unsigned,
    reordered, missing or rolled-back step, or a projection rewritten after the last
    receipt, fails closed.
    """
    receipts = _transition_receipts(events)
    previous_id, terminal, owner = None, None, None
    for event in events:
        step = _chain_step(authority, event, receipts)
        if step is None:
            continue
        kind, claims, receipt, start = step
        if claims.get("predecessor_receipt_id") != previous_id:
            raise ValueError("authenticated StateChart receipt chain is broken")
        if owner is not None and claims["owner_id"] != owner:
            raise ValueError("authenticated receipt chain changes owner")
        owner = claims["owner_id"]
        if previous_id is None and not start:
            raise ValueError("authenticated StateChart chain does not start at version zero")
        previous_id, terminal = receipt.event_id, (kind, claims, event, receipt)
    if terminal is None:
        raise ValueError("no StateChart transition to authenticate")
    _require_terminal(terminal, entity)
    return terminal[3]


def _transition_receipts(events) -> dict:
    from sose.core.transition_authority import ATTESTATION_EVENT

    receipts = {}
    for event in events:
        if event.name == ATTESTATION_EVENT:
            if event.causation_id in receipts:
                raise ValueError("a StateChart transition has two receipts")
            receipts[event.causation_id] = event
    return receipts


def _chain_step(authority, event: DomainEvent, receipts: dict):
    """(kind, claims, receipt, starts_the_chain) for a chain element, else None."""
    if event.name == "entity.state_transition":
        receipt = receipts.get(event.event_id)
        if receipt is None:
            raise ValueError("unattested StateChart transition: receipt missing")
        claims = authority._authenticate(receipt=receipt, event=event)
        start = claims.get("from_version") == 0 and not claims.get("predecessor_revision")
        return "transition", claims, receipt, start
    if event.name == ATTRIBUTES_EVENT:
        claims = authenticate_attributes(authority, event)
        if claims["predecessor_receipt_id"] != expected_attribute_predecessor(claims):
            raise ValueError("authenticated StateChart receipt chain is broken")
        return "attributes", claims, event, claims["version"] == 0 and claims["from_revision"] == 0
    return None


def _require_terminal(terminal, entity: Entity) -> None:
    from sose.core.transition_authority import _require_stored_projection

    kind, claims, source, _receipt = terminal
    if kind == "attributes":
        require_attribute_projection(claims, entity, None)
    elif entity.attribute_revision != 0:
        raise ValueError("authenticated StateChart receipt contradicts the stored entity")
    else:
        _require_stored_projection(claims, entity, source)

"""Pure claim-request validation, claimability and typed causal proof checks.

Split out of composition.boundary; BoundaryService keeps the transactions.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .model import BoundaryDelivery, DeliveryStatus


def validate_claim_request(
    *, owner_id: str, destination_domain: str | None, message_id: str | None,
    correlation_id: str | None, lease_duration: timedelta,
    accepted_contracts: frozenset[tuple[str, int]] | None,
) -> None:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id must be a nonblank string")
    for name, value in (
        ("destination_domain", destination_domain),
        ("message_id", message_id),
        ("correlation_id", correlation_id),
    ):
        if value is not None and not value:
            raise ValueError(f"{name} must be nonempty when specified")
    if lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")
    if accepted_contracts is not None and not all(
        _valid_contract(name, version) for name, version in accepted_contracts
    ):
        raise ValueError("accepted_contracts requires nonempty names and positive versions")


def _valid_contract(name: object, version: object) -> bool:
    return isinstance(name, str) and bool(name) and isinstance(version, int) and version > 0


def delivery_claimable(
    delivery: BoundaryDelivery, *, now: datetime, message_id: str | None,
    destination_domain: str | None, accepted_contracts: frozenset[tuple[str, int]] | None,
) -> bool:
    """Filters that need no message read: selection, contract, status and live lease."""
    if message_id is not None and delivery.message_id != message_id:
        return False
    if destination_domain is not None and (
        delivery.destination_domain != destination_domain
    ):
        return False
    if accepted_contracts is not None and (
        delivery.contract_name, delivery.contract_version
    ) not in accepted_contracts:
        return False
    if delivery.status is DeliveryStatus.CONSUMED:
        return False
    return not (
        delivery.status is DeliveryStatus.CLAIMED
        and delivery.lease_expires_at is not None
        and delivery.lease_expires_at > now
    )


def typed_predecessor_certified(message, predecessor, consumption, uow) -> bool:
    # Opt-in causal contract upgrade: for typed PC6 links, a missing
    # pending Command is not evidence that business application committed.
    # Legacy untyped v1 links preserve their original completion semantics.
    from .effects import _CERTIFIED_TERMINALS
    if message.causation_kind != "boundary" or predecessor.contract_key not in {
        expected[2] for expected in _CERTIFIED_TERMINALS.values()
    }:
        return True
    proof = uow.get_business_effect(consumption.consumer_effect_id)
    if proof is None:
        return False
    _require_terminal_proof(proof, predecessor, consumption, uow)
    return True


def _require_terminal_proof(proof, predecessor, consumption, uow) -> None:
    if (
        proof.effect_id != consumption.consumer_effect_id
        or proof.boundary_message_id != predecessor.message_id
        or proof.correlation_id != predecessor.correlation_id
    ):
        raise RuntimeError("typed causal predecessor certificate contradicts ACK")
    entity = uow.get_entity(proof.entity_type, proof.entity_id)
    if (
        entity is None
        or entity.version < proof.entity_version
        or entity.state != proof.terminal_state
    ):
        raise RuntimeError("typed causal predecessor certificate lacks durable terminal state")

"""Optional v2 organization binding carried by a ``BoundaryMessage`` (#496).

A v1 (domain-scoped) message has every binding field ``None`` and keeps its
historical identity derivation. A v2 message carries the complete binding,
which is folded into its deterministic identity; partial bindings and
identities that contradict the binding fail closed.
"""
from __future__ import annotations

import re
from typing import Any

from sose.core.identity import deterministic_id

ORGANIZATION_BINDING_FIELDS = (
    "organization_id",
    "definition_fingerprint",
    "config_epoch",
    "source_component_id",
    "source_owner_id",
    "destination_component_id",
    "destination_owner_id",
)
_IDENTITY_FIELDS = (
    "organization_id",
    "source_component_id",
    "source_owner_id",
    "destination_component_id",
    "destination_owner_id",
)
_ORGANIZATION_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def is_organization_scoped(binding: tuple[Any, ...]) -> bool:
    """Return whether ``binding`` is a complete v2 binding (False: all absent)."""
    if all(value is None for value in binding):
        return False
    if any(value is None for value in binding):
        raise ValueError("organizational boundary binding must be complete")
    return True


def organization_message_id(
    *,
    source_domain: str,
    source_identity: str,
    contract_name: str,
    contract_version: int,
    destination_domain: str,
    occurrence_key: str,
    binding: tuple[Any, ...],
) -> str:
    return deterministic_id(
        "boundary-message-organization-v2",
        source_domain,
        source_identity,
        contract_name,
        contract_version,
        destination_domain,
        occurrence_key,
        *binding,
    )


def binding_of(message: Any) -> tuple[Any, ...]:
    return tuple(getattr(message, name) for name in ORGANIZATION_BINDING_FIELDS)


def _require_canonical_identities(message: Any) -> None:
    for label in _IDENTITY_FIELDS:
        value = getattr(message, label)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{label} must be a canonical nonempty identity")


def _require_definition_pin(message: Any) -> None:
    epoch = message.config_epoch
    if type(epoch) is not int or epoch < 0:
        raise ValueError("config_epoch must be a nonnegative integer")
    fingerprint = message.definition_fingerprint
    if not isinstance(fingerprint, str) or not _ORGANIZATION_FINGERPRINT.fullmatch(
        fingerprint
    ):
        raise ValueError("definition_fingerprint must be a lowercase SHA-256")


def validate_organization_binding(message: Any) -> None:
    """Fail closed on a partial, malformed or identity-contradicting binding."""
    binding = binding_of(message)
    if not is_organization_scoped(binding):
        return
    _require_canonical_identities(message)
    _require_definition_pin(message)
    expected_id = organization_message_id(
        source_domain=message.source_domain,
        source_identity=message.source_identity,
        contract_name=message.contract_name,
        contract_version=message.contract_version,
        destination_domain=message.destination_domain,
        occurrence_key=message.occurrence_key,
        binding=binding,
    )
    if message.message_id != expected_id:
        raise ValueError("organizational boundary message identity contradicts binding")

"""Keep organization-scoped (v2) boundary messages behind their router (#496).

Generic domain-level publishing, claiming and dispatch remain byte-for-byte
unchanged for v1 messages. A v2 message carries an organization/component/
owner binding that only the organization facade verifies, so the generic
paths refuse or skip it unless that facade explicitly opts in.

``scoped_validator`` and ``organization_scope`` are hooks for trusted in-process
code, not a security boundary: a caller passing a permissive validator or another
component's scope bypasses the router, as direct unit-of-work writes would.
Persistence-level authority is #545's concern.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from sose.persistence.base import UnitOfWork

    from .model import BoundaryMessage

# (organization_id, destination_component_id, destination_owner_id)
OrganizationClaimScope = tuple[str, str, str]
ScopedPublicationValidator = Callable[["BoundaryMessage", "UnitOfWork"], None]


def reject_organization_scoped(message: "BoundaryMessage") -> None:
    from .boundary import UnsupportedBoundaryContractError

    if message.organization_scoped:
        raise UnsupportedBoundaryContractError(
            "organization-scoped boundary message requires the organization router: "
            f"{message.message_id}"
        )


def require_scoped_publication(
    message: "BoundaryMessage",
    uow: "UnitOfWork",
    validator: ScopedPublicationValidator | None,
) -> None:
    """Run the facade's owner/ancestry checks inside the publish transaction."""
    if not message.organization_scoped:
        if validator is not None:
            raise ValueError("scoped publication validator requires a v2 scoped message")
        return
    if validator is None:
        raise PermissionError(
            "organization-scoped boundary message must be published through "
            "the organization boundary facade"
        )
    validator(message, uow)


def message_in_claim_scope(
    message: "BoundaryMessage", scope: OrganizationClaimScope | None,
) -> bool:
    """v1 messages are claimable only unscoped; v2 only by their exact consumer."""
    if scope is None:
        return not message.organization_scoped
    return message.organization_scoped and scope == (
        message.organization_id,
        message.destination_component_id,
        message.destination_owner_id,
    )

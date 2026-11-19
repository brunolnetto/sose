"""#464: before a NEW OD-4 management decision, re-verify every earlier paid Command.

Each earlier paid decision event must still match the original Command that caused it:
identity, owner, entity, action, due/issued time, the work's deterministic flow
correlation, cause and frozen request. Replay requests keep their own stronger checks
in ``od4_intake``. This is a consistency check over a public unit of work, not an
issuer root (#545).
"""
from __future__ import annotations

from sose.core.identity import deterministic_id


def verify_original_management_commands(uow, owner_id: str, work_id: str, protocol) -> None:
    from sose.organizational.od4_intake import _DECISION_EVENT, WORK_KIND, _utc

    snapshot = protocol.snapshot()
    flow = deterministic_id("od4-work-flow", owner_id, work_id)
    for decision in uow.events():
        if (decision.name, decision.entity_type, decision.entity_id) != (
            _DECISION_EVENT, WORK_KIND, work_id,
        ):
            continue
        payload = decision.payload
        cause, action = payload.get("cause_id"), payload.get("action")
        observed = _utc(decision.occurred_at)
        command_id = deterministic_id("od4-command-v1", owner_id, work_id, cause)
        expected = (
            command_id, owner_id, WORK_KIND, work_id, action, observed, observed, None, flow,
            cause, snapshot,
            [action, payload.get("matched_action"), observed.isoformat(), snapshot],
        )
        command = uow.get_command(command_id)
        if decision.correlation_id != flow or _identity(command) != expected:
            raise ValueError("OD-4 durable management original Command inconsistent")


def _identity(command) -> tuple | None:
    """Every field of the original Command the paid decision must still match."""
    if command is None or not isinstance(command.payload, dict):
        return None
    payload = command.payload
    return (
        command.command_id, command.owner_id, command.entity_type, command.entity_id,
        command.name, command.due_at, command.issued_at, command.causation_id,
        command.correlation_id, payload.get("cause_id"), payload.get("protocol"),
        payload.get("request"),
    )

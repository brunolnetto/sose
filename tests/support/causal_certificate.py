"""Domain-neutral, immutable causal evidence for restart/concurrency falsification.

The oracle can be driven by a different scheduler: compare persisted facts
without relying on private runtime objects, event counts, or a running process.
Future domains can supply their source identity and physical event names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sose.core.identity import deterministic_id


@dataclass(frozen=True)
class CausalCertificate:
    sources: tuple[Any, ...]
    events: tuple[Any, ...]
    resource_definitions: tuple[Any, ...]
    resource_reservations: tuple[Any, ...]
    messages: tuple[Any, ...]
    deliveries: tuple[Any, ...]
    consumptions: tuple[Any, ...]
    all_consumptions: tuple[Any, ...]
    commands: tuple[Any, ...]
    all_commands: tuple[Any, ...]
    observations: tuple[Any, ...]
    all_observations: tuple[Any, ...]
    effects: tuple[Any, ...]
    all_effects: tuple[Any, ...]


def capture_causal_certificate(
    store: Any, *, organizations: Iterable[str], source_type: str,
    observation_type: str, physical_event_names: frozenset[str],
    command_identity_namespace: str,
) -> CausalCertificate:
    """Capture negative evidence (missing Command/certificate) as explicit None.

    The projection includes full causal DAG entries, clocks embedded in source
    entities, physical reservations/definitions, and all five downstream fact
    classes, for the supplied organizations only. Identity-sorted records are
    compared by complete value, not counts or summary metrics.
    """
    orgs = tuple(sorted(organizations))
    with store.transaction() as uow:
        sources = tuple(uow.get_entity(source_type, name) for name in orgs)
        events = tuple(sorted(
            (event for event in uow.events()
             if event.correlation_id in orgs
             or (event.entity_type == source_type and event.entity_id in orgs)),
            key=lambda item: item.event_id,
        ))
        deliveries = tuple(sorted(
            (
                delivery for delivery in uow.boundary_deliveries()
                if (message := uow.get_boundary_message(delivery.message_id)) is not None
                and message.correlation_id in orgs
            ),
            key=lambda item: item.delivery_id,
        ))
        # Enumerate every scoped publication independently of deliveries:
        # an orphan message with no delivery is itself causal corruption.
        messages = tuple(sorted(
            (m for m in uow.boundary_messages() if m.correlation_id in orgs),
            key=lambda m: m.message_id,
        ))
        physical = (event for event in events if event.name in physical_event_names)
        scoped_message_ids = frozenset(m.message_id for m in messages)
        scoped_delivery_ids = frozenset(d.delivery_id for d in deliveries)
        commands = tuple(
            (m.message_id, uow.get_command(deterministic_id(
                command_identity_namespace, m.message_id,
            )))
            for m in messages
        )
        effects = tuple(
            (m.message_id, uow.get_business_effect(deterministic_id(
                command_identity_namespace, m.message_id,
            )))
            for m in messages
        )
        return CausalCertificate(
            sources=sources,
            events=events,
            resource_definitions=tuple(sorted(
                (r for r in store.resource_definitions()
                 if any(r.name.startswith(name + "/") for name in orgs)),
                key=lambda item: item.name,
            )),
            resource_reservations=tuple(sorted(
                (r for r in uow.resource_reservations()
                 if any(r.resource_name.startswith(name + "/") for name in orgs)),
                key=lambda item: item.reservation_id,
            )),
            messages=messages,
            deliveries=deliveries,
            consumptions=tuple(
                (d.delivery_id, uow.get_boundary_consumption(d.delivery_id))
                for d in deliveries
            ),
            all_consumptions=tuple(sorted(
                (receipt for receipt in uow.boundary_consumptions()
                 if receipt.message_id in scoped_message_ids
                 or receipt.delivery_id in scoped_delivery_ids),
                key=lambda receipt: (receipt.delivery_id, receipt.consumption_id),
            )),
            commands=commands,
            # An unexpected staged command is executable domain intent, not
            # merely a missing transport receipt. Preserve the nullable
            # expected Command slots AND all organization-correlated ones.
            all_commands=tuple(sorted(
                (command for command in uow.commands()
                 if command.correlation_id in orgs),
                key=lambda command: command.command_id,
            )),
            observations=tuple(
                (e.event_id, uow.get_entity(observation_type, e.event_id))
                for e in physical
            ),
            all_observations=tuple(sorted(
                (entity for entity in uow.entities()
                 if entity.entity_type == observation_type
                 and entity.attributes.get("correlation_id") in orgs),
                key=lambda entity: entity.id,
            )),
            effects=effects,
            # Enumerate *every* scoped immutable effect, not only those
            # reachable through an expected message ID. An extra or
            # mis-keyed certificate must falsify causal equivalence.
            all_effects=tuple(sorted(
                (effect for effect in uow.business_effects()
                 if effect.correlation_id in orgs),
                key=lambda effect: (effect.effect_id, effect.receipt_id),
            )),
        )

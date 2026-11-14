"""Bounded owner-qualified observation of admitted organization event evidence.

This is the first #496 READ boundary, not executable organizational composition.
It never synthesizes a global organization clock, entity StateChart or mutable
organization aggregate; it does not grant a resource, install operational
policy/calendar/cost references, or certify a boundary effect. Full causal
queues, receipts, workers and physical resource state remain separate gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .organization import OrganizationDefinition, validate_organization_admission

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class ComponentEvidence:
    component_id: str
    domain: str
    owner_id: str
    entity_types: tuple[str, ...]
    event_ids: tuple[str, ...]
    declared_job_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrganizationEvidence:
    organization_id: str
    definition_fingerprint: str
    version: int
    config_epoch: int
    components: tuple[ComponentEvidence, ...]


def observe_organization_events(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    catalog,
    manifests,
    consumers,
) -> OrganizationEvidence:
    """Project one owner-qualified durable event snapshot without mutation.

    Runtime evidence is selected only after the actual catalog, audited
    manifests and installed boundary handlers admit the exact definition.
    The persistence adapter supplies one event tuple; its order is preserved
    *within* each owner/component only. This is NOT an atomic global snapshot
    of jobs/queues/physical grants across independent organizations.
    """
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
    )
    components = tuple(sorted(definition.components, key=lambda c: c.component_id))
    owner_ids = tuple(component.owner_id for component in components)
    if len(owner_ids) != len(set(owner_ids)):
        # v1 events only carry owner_id + entity_type, not a standalone
        # organization instance/component binding. Do not falsely attribute
        # two component instances with one owner ID to a unique source.
        raise ValueError("owner-qualified observation requires distinct component owner_id")

    types_by_owner = _component_entity_types(components, catalog)
    by_owner = _owner_transition_ids(persistence.events(), types_by_owner, owner_ids)

    return OrganizationEvidence(
        organization_id=definition.organization_id,
        definition_fingerprint=fingerprint,
        version=definition.version,
        config_epoch=definition.config_epoch,
        components=tuple(
            ComponentEvidence(
                component_id=component.component_id,
                domain=component.domain,
                owner_id=component.owner_id,
                entity_types=tuple(sorted(types_by_owner[component.owner_id])),
                event_ids=tuple(by_owner[component.owner_id]),
                declared_job_ids=tuple(sorted(
                    job.job_id for job in definition.jobs
                    if job.component_id == component.component_id
                )),
            )
            for component in components
        ),
    )


def _owner_transition_ids(events, types_by_owner, owner_ids) -> dict[str, list[str]]:
    """Owner -> transition event IDs, in event-tuple order within each owner."""
    all_known_types = set().union(*types_by_owner.values())
    by_owner: dict[str, list[str]] = {owner: [] for owner in owner_ids}
    seen_ids = set()
    # Never read per-owner clocks to infer a global cut or coordinate owners.
    # Store.events is the sole observation read; no UoW and no write.
    for event in events:
        if event.owner_id not in types_by_owner:
            continue
        if event.entity_type in types_by_owner[event.owner_id]:
            if _is_observed_transition(event):
                if event.event_id in seen_ids:
                    raise ValueError("duplicate durable organization event identity")
                seen_ids.add(event.event_id)
                by_owner[event.owner_id].append(event.event_id)
        elif event.entity_type in all_known_types:
            raise ValueError("owner/event StateChart type contradicts admitted component")
        # Other event families owned by this component (e.g. config-source
        # attestations) are not implicitly promoted into constituent events.
    return by_owner


def _component_entity_types(components, catalog) -> dict[str, frozenset[str]]:
    """Owner -> StateChart entity types its component may emit (in order)."""
    types_by_owner: dict[str, frozenset[str]] = {}
    for component in components:
        registered = catalog.get(component.domain).registered_entity_types
        types = frozenset(
            entry.name for entry in registered
            if entry.chart_factory.__name__ in component.statechart_types
        )
        if not types:
            raise ValueError("component observation requires registered entity types")
        types_by_owner[component.owner_id] = types
    return types_by_owner


_TRANSITION_FIELDS = ("trigger", "from_state", "to_state")


def _is_observed_transition(event) -> bool:
    """A well-formed StateChart transition; other event families are skipped.

    Same entity type does not make an arbitrary DomainEvent a StateChart
    transition. Enrollment/config events are distinct evidence families,
    even when emitted by the same entity.
    """
    if event.name != "entity.state_transition":
        return False
    if not all(
        isinstance(event.payload.get(field), str) and event.payload[field].strip()
        for field in _TRANSITION_FIELDS
    ):
        raise ValueError("malformed observed StateChart transition payload")
    return True

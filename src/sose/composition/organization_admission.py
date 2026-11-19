"""Static admission of an OrganizationDefinition against executable PC6 registrations.

Split out of composition.organization, which re-exports these names.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .organization import BoundaryBinding, OrganizationDefinition, ProcessComponent


def _reject_unadmitted_cycles(boundaries: tuple[BoundaryBinding, ...]) -> None:
    """Fail closed on feedback until a typed liveness policy is certified.

    Asynchronous business feedback can be legal, but a mere topology edge
    cannot claim a bounded/recoverable cycle without that separate contract.
    """
    edges: dict[str, set[str]] = {}
    for edge in boundaries:
        edges.setdefault(edge.producer_id, set()).add(edge.consumer_id)

    active: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            raise ValueError("cyclic organizational boundary requires liveness admission")
        if node in visited:
            return
        active.add(node)
        for destination in sorted(edges.get(node, ())):
            visit(destination)
        active.remove(node)
        visited.add(node)

    for component in sorted(edges):
        visit(component)


def validate_organization_admission(
    definition: OrganizationDefinition,
    *,
    catalog,
    manifests,
    consumers,
) -> str:
    """Certify a static definition against executable PC6 registrations.

    This is admission, not an organizational runner. The supplied domain
    catalog is used to instantiate isolated *unseeded* runtime registries;
    only actual registered StateChart factories are accepted. Real process
    manifests must attest both sides of every boundary, and the consumer
    handler must exist in the supplied BoundaryConsumerRegistry. An empty
    registry is never treated as "accept any contract".
    """
    from sose.examples.process_manifest import ProcessEvidence

    # Lazy: composition.organization re-exports this module's names, so a
    # top-level import here would make importing this module first fail.
    from .organization import OrganizationDefinition

    if not isinstance(definition, OrganizationDefinition):
        raise TypeError("organization admission requires OrganizationDefinition")
    _reject_unadmitted_cycles(definition.boundaries)
    if definition.operational_configs:
        # No trusted policy/calendar/cost registry is installed in the
        # current process manifests. Structural hashes are NOT executable
        # admission: otherwise an arbitrary caller can self-attest a policy
        # and accidentally change runtime cost, availability, or authority.
        raise ValueError(
            "operational policy/calendar/cost references require audited runtime admission"
        )

    if definition.physical_pools:
        # A PhysicalPoolRef supplies only caller-controlled names, not proof
        # that a real pool exists with the declared owner, units, instances
        # or capacity. Do not admit the organizational graph as executable
        # until a trusted, owner-qualified resource registry is installed.
        # Concrete physical grants stay authoritative in #477; runner binding
        # and configuration change fencing belong to #496.
        raise ValueError(
            "physical pool references require authoritative resource admission"
        )

    components = {component.component_id: component for component in definition.components}
    _reject_shared_boundary_domains(definition, components)
    validated_manifests = {
        component.component_id: _validated_manifest(component, catalog, manifests)
        for component in definition.components
    }
    for boundary in definition.boundaries:
        _require_boundary_attested(
            boundary, components, validated_manifests, consumers, ProcessEvidence,
        )

    return definition.fingerprint()


def _reject_shared_boundary_domains(definition: OrganizationDefinition, components) -> None:
    # Consumer registration is currently qualified by domain, not by the
    # independently owned component/instance. A domain appearing twice in
    # an active boundary would allow one handler to be mistaken for another
    # owner's executable ingress/egress. Reject until both admission and
    # dispatch carry the exact destination component/owner identity.
    boundary_domains = {
        components[endpoint].domain
        for boundary in definition.boundaries
        for endpoint in (boundary.producer_id, boundary.consumer_id)
    }
    domain_counts: dict[str, int] = {}
    for component in definition.components:
        domain_counts[component.domain] = domain_counts.get(component.domain, 0) + 1
    for domain in sorted(boundary_domains):
        if domain_counts[domain] > 1:
            raise ValueError(
                f"owner-qualified boundary registration required for "
                f"multiple {domain} components"
            )


def _validated_manifest(component: ProcessComponent, catalog, manifests):
    domain = catalog.get(component.domain)
    manifest = manifests.get(component.domain)
    if (
        manifest is None
        or manifest.domain != component.domain
        or not manifest.assessment_complete
    ):
        raise ValueError(
            f"missing audited process manifest for {component.domain}"
        )

    # A validator must NEVER run default_config/build_runtime: those are
    # unconstrained callables and may have arbitrary external side effects.
    if component.process_version != domain.process_version:
        raise ValueError(
            f"unsupported process_version for {component.domain}: {component.process_version}"
        )
    # Builtin ProcessManifest entries are currently domain-keyed, not
    # version-keyed. They attest the v1 process only; matching a v2
    # DomainDefinition must never inherit v1 audited evidence.
    if component.process_version != 1:
        raise ValueError(
            f"version-specific process manifest unavailable for "
            f"{component.domain} v{component.process_version}"
        )
    chart_names = frozenset(
        getattr(entry.chart_factory, "__name__", "")
        for entry in domain.registered_entity_types
    )
    unknown = set(component.statechart_types) - chart_names
    if unknown:
        raise ValueError(
            f"unregistered StateChart for {component.domain}: {sorted(unknown)}"
        )
    return manifest


def _require_boundary_attested(
    boundary: BoundaryBinding, components, validated_manifests, consumers, evidence,
) -> None:
    producer = components[boundary.producer_id]
    consumer = components[boundary.consumer_id]
    contract = f"{boundary.contract_name}.v{boundary.contract_version}"
    source = validated_manifests[boundary.producer_id]
    destination = validated_manifests[boundary.consumer_id]
    if (evidence.EGRESS_CONTRACTS not in source.evidence
            or contract not in source.egress_contracts
            or not source.contract_sources("egress", contract)):
        raise ValueError(
            f"missing egress manifest for {producer.domain}: {contract}"
        )
    if (evidence.INGRESS_CONTRACTS not in destination.evidence
            or contract not in destination.ingress_contracts
            or not destination.contract_sources("ingress", contract)):
        raise ValueError(
            f"missing ingress manifest for {consumer.domain}: {contract}"
        )
    if (
        boundary.contract_name, boundary.contract_version
    ) not in consumers.contracts_for_source(
        source_domain=producer.domain, destination_domain=consumer.domain,
    ):
        raise ValueError(
            f"unregistered consumer contract for {consumer.domain}: {contract}"
        )

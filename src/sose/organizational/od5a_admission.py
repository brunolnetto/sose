"""OD-5a pre-execution mechanism and equal-budget admission contracts.

This compiler is deliberately *not* a simulation runner: it constructs
hash-addressed ModelSpecs, proves what each arm can change, and rejects
unpaid or ambiguously priced interventions. No confirmatory outcomes are
generated before the independent source+parameters execution freeze.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite

from .model_spec import InterventionClass, ModelIntervention, ModelSpec


TREATMENT_ARMS = (
    "capacity_only", "topology_only", "coordination_only",
    "structural_combined",
)
ALL_ARMS = ("baseline", *TREATMENT_ARMS)


@dataclass(frozen=True, slots=True)
class OD5aChargedCost:
    """Full declared lifecycle charge (NOT an observed post-treatment outcome)."""

    operating_cost_at_horizon: float
    transition_cost: float
    transition_time: float
    shadow_price_per_transition_time: float

    def __post_init__(self) -> None:
        for field in (
            "operating_cost_at_horizon", "transition_cost",
            "transition_time", "shadow_price_per_transition_time",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError(f"{field} must be a numeric declared charge")
            if not isfinite(value) or value < 0:
                raise ValueError(f"{field} must be finite and nonnegative")
        if self.transition_time > 0 and self.shadow_price_per_transition_time <= 0:
            raise ValueError("transition time must have a positive shadow price")
        if not isfinite(self.total):
            raise ValueError("total lifecycle budget must be finite")

    @property
    def total(self) -> float:
        return (
            self.operating_cost_at_horizon
            + self.transition_cost
            + self.transition_time * self.shadow_price_per_transition_time
        )


@dataclass(frozen=True, slots=True, init=False)
class OD5aCompiledArm:
    """Admission truth is immutable even if a caller mutates an inspection copy.

    Pydantic's frozen ModelIntervention freezes attributes, not the nested
    set_values and mechanisms_changed collections. Store a canonical immutable
    snapshot and return a fresh validated intervention to each consumer.
    """

    arm_id: str
    model: ModelSpec
    charged_cost: OD5aChargedCost | None
    _intervention_json: str | None

    def __init__(
        self, arm_id: str, model: ModelSpec, charged_cost: OD5aChargedCost | None,
        intervention: ModelIntervention | None,
    ) -> None:
        object.__setattr__(self, "arm_id", arm_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "charged_cost", charged_cost)
        frozen_payload = (
            None if intervention is None
            else json.dumps(
                intervention.canonical_payload(), sort_keys=True,
                separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            )
        )
        object.__setattr__(self, "_intervention_json", frozen_payload)

    @property
    def intervention(self) -> ModelIntervention | None:
        """New independent value object; editing it cannot change admission."""
        if self._intervention_json is None:
            return None
        return ModelIntervention.model_validate(json.loads(self._intervention_json))

    @property
    def intervention_hash(self) -> str | None:
        if self._intervention_json is None:
            return None
        return sha256(self._intervention_json.encode("utf-8")).hexdigest()

    @property
    def admission_hash(self) -> str:
        data = {
            "arm_id": self.arm_id,
            "model_spec_hash": self.model.model_spec_hash,
            "intervention_hash": self.intervention_hash,
            "cost": (
                None if self.charged_cost is None
                else {
                    "operating_cost_at_horizon": self.charged_cost.operating_cost_at_horizon,
                    "transition_cost": self.charged_cost.transition_cost,
                    "transition_time": self.charged_cost.transition_time,
                    "shadow_price_per_transition_time":
                        self.charged_cost.shadow_price_per_transition_time,
                    "total": self.charged_cost.total,
                }
            ),
        }
        return sha256(json.dumps(
            data, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()


def _edges(model: ModelSpec) -> tuple[tuple[str, str], ...]:
    raw = model.routing.get("dependency_edges")
    if not isinstance(raw, tuple) or not raw:
        raise ValueError("baseline must define a nonempty dependency_edges graph")
    edges = []
    for edge in raw:
        if (not isinstance(edge, tuple) or len(edge) != 2
                or not all(isinstance(part, str) and part.strip() for part in edge)
                or edge[0] == edge[1]):
            raise ValueError("dependency_edges require pairs of distinct actor/station IDs")
        edges.append(tuple(edge))
    if len(set(edges)) != len(edges):
        raise ValueError("dependency_edges may not contain duplicate edges")
    _assert_dag(tuple(edges))
    return tuple(edges)


def _assert_dag(edges: tuple[tuple[str, str], ...]) -> None:
    children: dict[str, set[str]] = {}
    for source, target in edges:
        children.setdefault(source, set()).add(target)
        children.setdefault(target, set())
    active: set[str] = set()
    complete: set[str] = set()

    def visit(vertex: str) -> None:
        if vertex in active:
            raise ValueError("dependency graph must remain acyclic")
        if vertex in complete:
            return
        active.add(vertex)
        for child in children[vertex]:
            visit(child)
        active.remove(vertex)
        complete.add(vertex)

    for vertex in children:
        visit(vertex)


def _parameter(model: ModelSpec, name: str, *, strictly_positive: bool) -> float:
    value = model.parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"baseline parameter {name} must be numeric")
    if not isfinite(value) or value < 0 or (strictly_positive and value <= 0):
        raise ValueError(f"baseline parameter {name} is outside its physical range")
    if name not in model.parameter_evidence:
        raise ValueError(f"baseline parameter {name} lacks evidence class")
    return float(value)


def compile_od5a_arms(
    *,
    baseline: ModelSpec,
    expanded_capacity: float,
    removed_dependency_edges: tuple[tuple[str, str], ...],
    reduced_coordination_latency: float,
    declared_costs: Mapping[str, OD5aChargedCost],
    budget_tolerance_fraction: float = 0.01,
) -> tuple[OD5aCompiledArm, ...]:
    """Admit the five fixed arms or fail before any stochastic execution.

    Only capacity, dependency graph, and configured coordination latency may
    differ. No simulation outcomes, realized queues, or service measurements
    are inputs to this function.
    """
    original_capacity = _parameter(baseline, "execution_slots", strictly_positive=True)
    if not original_capacity.is_integer():
        raise ValueError("execution_slots must represent an integer number of physical slots")
    original_latency = _parameter(
        baseline, "coordination_latency", strictly_positive=True,
    )
    _parameter(baseline, "arrival_rate", strictly_positive=True)
    original_edges = _edges(baseline)

    if (isinstance(expanded_capacity, bool)
            or not isinstance(expanded_capacity, int)
            or expanded_capacity <= original_capacity):
        raise ValueError("capacity-only arm must strictly increase execution capacity")
    if (isinstance(reduced_coordination_latency, bool)
            or not isinstance(reduced_coordination_latency, (int, float))
            or not isfinite(reduced_coordination_latency)
            or not 0 <= reduced_coordination_latency < original_latency):
        raise ValueError("coordination intervention must strictly reduce latency")

    removed = tuple(removed_dependency_edges)
    if len(removed) != len(set(removed)) or not removed:
        raise ValueError("removed_dependency_edges must be nonempty and unique")
    if not set(removed) <= set(original_edges):
        raise ValueError("structural arm cannot invent or remove absent edges")
    remaining = tuple(edge for edge in original_edges if edge not in set(removed))
    if not remaining:
        raise ValueError("structural arm cannot eliminate all dependency edges")
    _assert_dag(remaining)

    if set(declared_costs) != set(TREATMENT_ARMS):
        raise ValueError("exactly four treated arm cost ledgers are required")
    if (not isfinite(budget_tolerance_fraction)
            or not 0 <= budget_tolerance_fraction <= 0.01):
        raise ValueError("matched-cost tolerance cannot exceed preregistered 1%")
    for arm, cost in declared_costs.items():
        if not isinstance(cost, OD5aChargedCost):
            raise ValueError(f"{arm} must have a typed charged cost ledger")
    reference_total = declared_costs["capacity_only"].total
    if reference_total <= 0:
        raise ValueError("capacity-only treatment must have a positive priced budget")
    for arm, cost in declared_costs.items():
        if abs(cost.total - reference_total) / reference_total > budget_tolerance_fraction:
            raise ValueError(f"{arm} treatment breaches matched lifecycle cost budget")

    mutations = {
        "capacity_only": (
            InterventionClass.CAPACITY,
            {"/parameters/execution_slots": expanded_capacity},
            ("execution_capacity",),
        ),
        "topology_only": (
            InterventionClass.STRUCTURAL,
            {"/routing/dependency_edges": [list(edge) for edge in remaining]},
            ("dependency_edges",),
        ),
        "coordination_only": (
            InterventionClass.STRUCTURAL,
            {"/parameters/coordination_latency": float(reduced_coordination_latency)},
            ("coordination_latency",),
        ),
        "structural_combined": (
            InterventionClass.STRUCTURAL,
            {
                "/routing/dependency_edges": [list(edge) for edge in remaining],
                "/parameters/coordination_latency": float(reduced_coordination_latency),
            },
            ("dependency_edges", "coordination_latency"),
        ),
    }

    result = [OD5aCompiledArm("baseline", baseline, None, None)]
    initial = baseline.canonical_payload()
    for arm in TREATMENT_ARMS:
        intervention_class, edits, mechanisms = mutations[arm]
        cost = declared_costs[arm]
        intervention = ModelIntervention(
            intervention_id=arm,
            intervention_class=intervention_class,
            set_values=edits,
            mechanisms_changed=list(mechanisms),
            # We don't repurpose ModelIntervention.operating_cost as a rate:
            # the fully charged lifecycle cost lives in OD5aChargedCost.
            operating_cost=cost.operating_cost_at_horizon,
            transition_cost=cost.transition_cost,
            transition_time=cost.transition_time,
        )
        model, _ = intervention.apply(baseline)
        expected = json.loads(json.dumps(initial))
        for key, value in edits.items():
            section, name = key.lstrip("/").split("/")
            expected[section][name] = value
        if model.canonical_payload() != expected:
            raise ValueError(f"{arm} changed a mechanism outside its registered treatment")
        if model.model_spec_hash == baseline.model_spec_hash:
            raise ValueError(f"{arm} is a no-op intervention")
        result.append(OD5aCompiledArm(arm, model, cost, intervention))
    return tuple(result)

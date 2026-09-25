from __future__ import annotations

from datetime import datetime
from functools import reduce
from operator import mul
from typing import Any, Callable

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource

from .model import (
    AttributeEffect,
    Scenario,
    ScenarioActivation,
    ScenarioDecision,
    ScenarioEvaluation,
    ScenarioSignal,
    ScenarioSignalKind,
    TransitionWeightEffect,
    iter_leaf_effects,
)


class ScenarioEngine:
    """Deterministic external-condition runtime.

    Scenarios modify simulation context. They never select or execute StateChart
    transitions directly; transition legality and stochastic branch selection remain
    owned by StateCharts and the probabilistic transition runtime.
    """

    def __init__(
        self,
        *,
        random_source: RandomSource,
        now: Callable[[], datetime],
        tick: Callable[[], int],
        context: Callable[[], Any],
    ) -> None:
        self._random = random_source
        self._now = now
        self._tick = tick
        self._context = context
        self._scenarios: dict[str, Scenario] = {}
        self._decisions: dict[str, ScenarioDecision] = {}
        self._activations: dict[str, ScenarioActivation] = {}

    def register(self, scenario: Scenario) -> Scenario:
        if scenario.name in self._scenarios:
            raise ValueError(f"scenario already registered: {scenario.name}")
        self._scenarios[scenario.name] = scenario
        return scenario

    def register_many(self, scenarios) -> None:
        for scenario in scenarios:
            self.register(scenario)

    @property
    def active_activations(self) -> tuple[ScenarioActivation, ...]:
        now = self._now()
        return tuple(
            activation
            for activation in self._ordered_activations()
            if activation.expires_at is None or activation.expires_at > now
        )

    def _ordered_activations(self) -> tuple[ScenarioActivation, ...]:
        return tuple(self._activations[key] for key in sorted(self._activations))

    @property
    def decisions(self) -> tuple[ScenarioDecision, ...]:
        return tuple(self._decisions[key] for key in sorted(self._decisions))

    def on_tick(self) -> tuple[ScenarioDecision, ...]:
        return self.evaluate(
            ScenarioSignal(
                kind=ScenarioSignalKind.TICK,
                now=self._now(),
                tick=self._tick(),
            )
        )

    def on_event(self, event: DomainEvent) -> tuple[ScenarioDecision, ...]:
        return self.evaluate(
            ScenarioSignal(
                kind=ScenarioSignalKind.EVENT,
                now=self._now(),
                tick=self._tick(),
                event=event,
            )
        )

    def evaluate(self, signal: ScenarioSignal) -> tuple[ScenarioDecision, ...]:
        self.expire_due(signal.now)
        decisions: list[ScenarioDecision] = []
        scenarios = sorted(self._scenarios.values(), key=lambda s: (s.priority, s.name))
        for scenario in scenarios:
            if not scenario.trigger.matches(signal):
                continue

            trigger_key = scenario.trigger.key(signal)
            attempt_id = deterministic_id("scenario-attempt", scenario.name, *trigger_key)
            previous = self._decisions.get(attempt_id)
            if previous is not None:
                decisions.append(previous)
                continue

            if not scenario.allow_reentry and any(
                activation.scenario_name == scenario.name
                for activation in self._activations.values()
            ):
                decision = ScenarioDecision(
                    attempt_id=attempt_id,
                    scenario_name=scenario.name,
                    activated=False,
                    probability=scenario.activation_probability,
                    draw=None,
                    reason="already_active",
                )
                self._decisions[attempt_id] = decision
                decisions.append(decision)
                continue

            evaluation = ScenarioEvaluation(
                scenario=scenario,
                signal=signal,
                context=self._context(),
            )
            if scenario.condition is not None and not scenario.condition(evaluation):
                decision = ScenarioDecision(
                    attempt_id=attempt_id,
                    scenario_name=scenario.name,
                    activated=False,
                    probability=scenario.activation_probability,
                    draw=None,
                    reason="condition_false",
                )
                self._decisions[attempt_id] = decision
                decisions.append(decision)
                continue

            draw = self._random.for_scope(
                "scenario-activation",
                scenario.name,
                *trigger_key,
            ).random()
            activated = draw < scenario.activation_probability
            activation_id = None
            reason = "probability_miss"

            if activated:
                activation_id = deterministic_id("scenario-activation", attempt_id)
                event = signal.event
                activation = ScenarioActivation(
                    activation_id=activation_id,
                    scenario_name=scenario.name,
                    activated_at=signal.now,
                    expires_at=(
                        signal.now + scenario.duration
                        if scenario.duration is not None
                        else None
                    ),
                    priority=scenario.priority,
                    effects=scenario.effects,
                    trigger_key=trigger_key,
                    causation_id=event.event_id if event is not None else None,
                    correlation_id=(
                        (event.correlation_id or event.event_id)
                        if event is not None
                        else None
                    ),
                )
                self._activations[activation_id] = activation
                reason = "activated"

            decision = ScenarioDecision(
                attempt_id=attempt_id,
                scenario_name=scenario.name,
                activated=activated,
                probability=scenario.activation_probability,
                draw=draw,
                reason=reason,
                activation_id=activation_id,
            )
            self._decisions[attempt_id] = decision
            decisions.append(decision)

        return tuple(decisions)

    def expire_due(self, at: datetime | None = None) -> tuple[ScenarioActivation, ...]:
        now = at or self._now()
        expired = tuple(
            activation
            for activation in self._ordered_activations()
            if activation.expires_at is not None and activation.expires_at <= now
        )
        for activation in expired:
            self._activations.pop(activation.activation_id, None)
        return expired

    def attribute(self, key: str, default: Any = None) -> Any:
        matches: list[tuple[ScenarioActivation, AttributeEffect]] = []
        for activation in self.active_activations:
            for effect in iter_leaf_effects(activation.effects):
                if isinstance(effect, AttributeEffect) and effect.key == key:
                    matches.append((activation, effect))
        if not matches:
            return default

        best_priority = min(activation.priority for activation, _ in matches)
        matches = [item for item in matches if item[0].priority == best_priority]
        latest_at = max(activation.activated_at for activation, _ in matches)
        matches = [item for item in matches if item[0].activated_at == latest_at]
        activation, effect = min(matches, key=lambda item: item[0].activation_id)
        return effect.value

    def transition_weight_multiplier(self, entity_type: str, event: str) -> float:
        multipliers: list[float] = []
        for activation in self.active_activations:
            for effect in iter_leaf_effects(activation.effects):
                if not isinstance(effect, TransitionWeightEffect):
                    continue
                if effect.event != event:
                    continue
                if effect.entity_type is not None and effect.entity_type != entity_type:
                    continue
                multipliers.append(effect.multiplier)
        return reduce(mul, multipliers, 1.0)

    def transform_transition_weight(self, evaluation, base_weight: float) -> float:
        return float(base_weight) * self.transition_weight_multiplier(
            evaluation.entity.entity_type,
            evaluation.event,
        )

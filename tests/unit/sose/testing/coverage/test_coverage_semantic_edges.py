from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import random
from types import SimpleNamespace

import pytest

from sose.core.events import DomainEvent
from sose.domain.entity import Entity
from sose.probability.graph import (
    NoProbabilisticTransition,
    ProbabilisticTransitionGraph,
)
from sose.probability.model import (
    StateConfiguration,
    TransitionEdge,
    TransitionOption,
)
from sose.probability.policy import (
    CallableWeight,
    ConstantWeight,
    TransitionEvaluation,
    TransitionPolicy,
    _resolve,
    probabilistic,
    probabilistic_transitions,
)
from sose.probability.runtime import (
    ProbabilisticTransitionRuntime,
    _configuration_values,
    _enabled_events,
    _event_name,
)
from sose.scenarios.model import (
    AttributeEffect,
    CompositeEffect,
    EventTrigger,
    Scenario,
    ScenarioSignal,
    ScenarioSignalKind,
    ScheduledTrigger,
    TickTrigger,
    TransitionWeightEffect,
    iter_leaf_effects,
)
from sose.scenarios.rules import ScenarioRule
from sose.sinks.base import SinkBinding
from sose.sinks.model import AnalyticalBatch, SinkCheckpoint, SinkDelivery
from sose.statecharts.topology import (
    _event_id,
    _state_id,
    graph_from_statechart,
    policy_from_statechart,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITY = Entity(id="1", entity_type="order", state="open")


def _event(
    event_id: str = "e1",
    *,
    name: str = "created",
    entity_type: str = "order",
) -> DomainEvent:
    return DomainEvent(
        event_id=event_id,
        name=name,
        entity_type=entity_type,
        entity_id="1",
        occurred_at=NOW,
    )


def _evaluation(event: str = "go") -> TransitionEvaluation:
    edge = TransitionEdge("edge", "open", ("closed",), event)
    return TransitionEvaluation(
        event=event,
        configuration=StateConfiguration(("open",)),
        edges=(edge,),
        entity=ENTITY,
        context=SimpleNamespace(),
    )


def test_state_configuration_normalizes_and_validates():
    config = StateConfiguration(("b", "a", "a"))
    assert config.states == ("a", "b")
    assert config.contains("a")
    assert not config.contains("missing")
    assert StateConfiguration.from_values("open").states == ("open",)
    assert StateConfiguration.from_values([1, "two"]).states == ("1", "two")
    with pytest.raises(ValueError, match="cannot be empty"):
        StateConfiguration(())


def test_transition_edge_validation_and_availability():
    config = StateConfiguration(("parent", "leaf"))
    edge = TransitionEdge(
        "edge",
        "parent",
        [1, "target"],
        "go",
        metadata={"x": 1},
    )
    assert edge.targets == ("1", "target")
    assert dict(edge.metadata) == {"x": 1}
    assert edge.available_from(config)
    assert not TransitionEdge("other", "missing", (), None).available_from(config)

    with pytest.raises(ValueError, match="edge_id cannot be empty"):
        TransitionEdge("", "source", (), None)
    with pytest.raises(ValueError, match="edge source cannot be empty"):
        TransitionEdge("id", "", (), None)


def test_transition_policy_supports_all_weight_spec_kinds():
    evaluation = _evaluation()

    assert ConstantWeight(2).weight(evaluation) == 2.0
    assert CallableWeight(lambda ev: 3).weight(evaluation) == 3.0
    assert _resolve(4, evaluation) == 4.0
    assert _resolve(ConstantWeight(5), evaluation) == 5.0
    assert _resolve(lambda ev: 6, evaluation) == 6.0
    with pytest.raises(TypeError, match="unsupported transition weight"):
        _resolve(object(), evaluation)

    policy = TransitionPolicy(
        {"go": 2},
        default_weight=3,
        excluded_events="skip",
    )
    assert policy.has_explicit_weight("go")
    assert not policy.has_explicit_weight("other")
    assert policy.is_probabilistically_eligible("go")
    assert not policy.is_probabilistically_eligible("skip")
    assert policy.weight_for(evaluation) == 2.0
    assert policy.events == ("go",)
    assert policy.excluded_events == ("skip",)
    assert TransitionPolicy(default_weight=3).weight_for(_evaluation("other")) == 3.0

    strict = TransitionPolicy({}, strict=True)
    with pytest.raises(KeyError, match="no transition weight configured"):
        strict.weight_for(evaluation)

    built = probabilistic({"go": 2}, strict=False)
    assert isinstance(built, TransitionPolicy)
    assert built.weight_for(evaluation) == 2.0


def test_probabilistic_transitions_decorator_attaches_policy():
    decorator = probabilistic_transitions(
        {"go": 2},
        default_weight=0.5,
        strict=True,
        excluded_events=("skip",),
    )

    class Chart:
        pass

    result = decorator(Chart)
    assert result is Chart
    assert isinstance(Chart.sose_transition_policy, TransitionPolicy)
    assert Chart.sose_transition_policy.excluded_events == ("skip",)


def test_probabilistic_graph_constructor_and_event_filtering():
    e1 = TransitionEdge("1", "open", ("closed",), "close")
    e2 = TransitionEdge("2", "open", ("open",), None)
    e3 = TransitionEdge("3", "closed", ("open",), "reopen")

    with pytest.raises(ValueError, match="at least one edge"):
        ProbabilisticTransitionGraph(())
    with pytest.raises(ValueError, match="edge ids must be unique"):
        ProbabilisticTransitionGraph((e1, e1))

    graph = ProbabilisticTransitionGraph((e1, e2, e3))
    config = StateConfiguration(("open",))
    assert graph.edges == (e1, e2, e3)
    assert graph.outgoing(config) == (e1, e2)
    assert graph.event_edges(config) == {"close": (e1,)}
    assert graph.event_edges(config, enabled_events=("other",)) == {}


@pytest.mark.parametrize("weight", [math.inf, -math.inf, math.nan])
def test_distribution_rejects_non_finite_weights(weight):
    graph = ProbabilisticTransitionGraph(
        (TransitionEdge("1", "open", ("closed",), "go"),)
    )
    with pytest.raises(ValueError, match="must be finite"):
        graph.distribution(
            configuration=StateConfiguration(("open",)),
            policy=TransitionPolicy({"go": weight}),
            entity=ENTITY,
            context=SimpleNamespace(),
        )


def test_distribution_rejects_negative_and_zero_total_and_applies_transform():
    graph = ProbabilisticTransitionGraph(
        (
            TransitionEdge("1", "open", ("a",), "a"),
            TransitionEdge("2", "open", ("b",), "b"),
            TransitionEdge("3", "open", ("c",), "excluded"),
        )
    )
    config = StateConfiguration(("open",))

    with pytest.raises(ValueError, match="cannot be negative"):
        graph.distribution(
            configuration=config,
            policy=TransitionPolicy({"a": -1, "b": 0}),
            entity=ENTITY,
            context=SimpleNamespace(),
        )

    with pytest.raises(NoProbabilisticTransition, match="no positive-weight"):
        graph.distribution(
            configuration=config,
            policy=TransitionPolicy({"a": 0, "b": 0}),
            entity=ENTITY,
            context=SimpleNamespace(),
            enabled_events=("a", "b"),
        )

    calls = []
    options = graph.distribution(
        configuration=config,
        policy=TransitionPolicy(
            {"a": 1, "b": 2},
            excluded_events=("excluded",),
        ),
        entity=ENTITY,
        context=SimpleNamespace(),
        weight_transform=lambda evaluation, weight: (
            calls.append((evaluation.event, weight)) or weight * 2
        ),
    )
    assert calls == [("a", 1.0), ("b", 2.0)]
    assert [(o.event, o.weight) for o in options] == [("a", 2.0), ("b", 4.0)]
    assert sum(o.probability for o in options) == pytest.approx(1.0)


class _FixedRng:
    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


def test_graph_sample_covers_first_match_and_fallback():
    config = StateConfiguration(("open",))
    options = (
        TransitionOption(
            "a",
            1,
            0.4,
            (TransitionEdge("1", "open", ("a",), "a"),),
        ),
        TransitionOption(
            "b",
            1,
            0.4,
            (TransitionEdge("2", "open", ("b",), "b"),),
        ),
    )
    graph = ProbabilisticTransitionGraph(
        tuple(edge for option in options for edge in option.edges)
    )

    with pytest.raises(NoProbabilisticTransition, match="empty distribution"):
        graph.sample((), rng=_FixedRng(0), configuration=config)

    first = graph.sample(options, rng=_FixedRng(0.1), configuration=config)
    assert first.event == "a"
    fallback = graph.sample(options, rng=_FixedRng(0.95), configuration=config)
    assert fallback.event == "b"
    assert fallback.draw == 0.95


class _RandomSource:
    def __init__(self):
        self.scope = None

    def for_scope(self, *scope):
        self.scope = scope
        return random.Random(1)


def test_probability_runtime_helpers_and_decision_path():
    assert tuple(_configuration_values(SimpleNamespace(configuration_values=["a", 2]))) == (
        "a",
        "2",
    )
    assert tuple(_configuration_values(SimpleNamespace(current_state_value="open"))) == (
        "open",
    )
    with pytest.raises(TypeError, match="must expose"):
        tuple(_configuration_values(SimpleNamespace()))

    assert _event_name(SimpleNamespace(id="by-id", name="ignored")) == "by-id"
    assert _event_name(SimpleNamespace(id=None, name="by-name")) == "by-name"
    assert _event_name("raw") == "raw"
    assert _enabled_events(SimpleNamespace(), {}) is None
    assert _enabled_events(
        SimpleNamespace(
            enabled_events=lambda **kwargs: [
                SimpleNamespace(id="a"),
                SimpleNamespace(name="b"),
                "c",
            ]
        ),
        {"x": 1},
    ) == {"a", "b", "c"}

    graph = ProbabilisticTransitionGraph(
        (TransitionEdge("1", "open", ("closed",), "close"),)
    )
    policy = TransitionPolicy({"close": 1})
    source = _RandomSource()
    chart = SimpleNamespace(
        current_state_value="open",
        enabled_events=lambda **kwargs: ("close",),
    )
    context = SimpleNamespace(
        scenarios=SimpleNamespace(
            transform_transition_weight=lambda evaluation, weight: weight
        )
    )
    runtime = ProbabilisticTransitionRuntime(random_source=source, tick=lambda: 7)
    decision = runtime.decide(
        chart=chart,
        graph=graph,
        policy=policy,
        entity=ENTITY,
        context=context,
        guard_kwargs={"allowed": True},
        scope=("extra",),
    )
    assert decision.event == "close"
    assert source.scope == (
        "transition",
        7,
        "order",
        "1",
        "open",
        "extra",
    )


def test_scenario_trigger_validation_and_matching():
    with pytest.raises(ValueError, match="every must be >= 1"):
        TickTrigger(every=0)
    with pytest.raises(ValueError, match="offset cannot be negative"):
        TickTrigger(offset=-1)

    tick = ScenarioSignal(ScenarioSignalKind.TICK, NOW, 4)
    assert TickTrigger(every=2).matches(tick)
    assert not TickTrigger(every=3).matches(tick)
    assert TickTrigger().key(tick) == ("tick", 4)

    event_signal = ScenarioSignal(ScenarioSignalKind.EVENT, NOW, 4, _event())
    assert EventTrigger().matches(event_signal)
    assert EventTrigger(event="created").matches(event_signal)
    assert EventTrigger(entity_type="order").matches(event_signal)
    assert not EventTrigger(event="other").matches(event_signal)
    assert not EventTrigger(entity_type="invoice").matches(event_signal)
    assert not EventTrigger().matches(tick)
    assert EventTrigger().key(event_signal) == ("event", "e1")
    with pytest.raises(ValueError, match="requires an event"):
        EventTrigger().key(tick)

    scheduled = ScheduledTrigger(NOW + timedelta(hours=1))
    assert not scheduled.matches(tick)
    assert scheduled.matches(
        ScenarioSignal(ScenarioSignalKind.TICK, NOW + timedelta(hours=2), 5)
    )
    assert not scheduled.matches(event_signal)
    assert scheduled.key(tick) == ("scheduled", scheduled.at)


def test_scenario_effect_and_scenario_validation():
    with pytest.raises(ValueError, match="key cannot be empty"):
        AttributeEffect("", 1)

    with pytest.raises(ValueError, match="event cannot be empty"):
        TransitionWeightEffect("", 1)
    for multiplier in (-1, math.inf, math.nan):
        with pytest.raises(ValueError, match="must be finite"):
            TransitionWeightEffect("go", multiplier)
    with pytest.raises(ValueError, match="entity_type cannot be empty"):
        TransitionWeightEffect("go", 1, entity_type="")
    assert TransitionWeightEffect("go", 2).multiplier == 2.0

    with pytest.raises(ValueError, match="cannot be empty"):
        CompositeEffect(())
    nested = CompositeEffect(
        (
            AttributeEffect("a", 1),
            CompositeEffect((TransitionWeightEffect("go", 2),)),
        )
    )
    assert list(iter_leaf_effects((nested,))) == [
        AttributeEffect("a", 1),
        TransitionWeightEffect("go", 2),
    ]

    trigger = TickTrigger()
    effect = AttributeEffect("x", 1)
    with pytest.raises(ValueError, match="name cannot be empty"):
        Scenario("", trigger, (effect,))
    with pytest.raises(ValueError, match="at least one effect"):
        Scenario("name", trigger, ())
    for probability in (-0.1, 1.1, math.inf, math.nan):
        with pytest.raises(ValueError, match="between 0 and 1"):
            Scenario("name", trigger, (effect,), activation_probability=probability)
    with pytest.raises(ValueError, match="duration must be positive"):
        Scenario("name", trigger, (effect,), duration=timedelta(0))

    scenario = Scenario("name", trigger, [effect], activation_probability=0.5)
    assert scenario.effects == (effect,)
    assert scenario.activation_probability == 0.5


def test_legacy_scenario_rule_short_circuits_and_decides():
    calls = []
    false_rule = ScenarioRule(
        "false",
        lambda entity, context: False,
        lambda entity, context: calls.append("decide") or "go",
    )
    assert false_rule.evaluate(ENTITY, SimpleNamespace()) is None
    assert calls == []

    true_rule = ScenarioRule(
        "true",
        lambda entity, context: True,
        lambda entity, context: "go",
    )
    assert true_rule.evaluate(ENTITY, SimpleNamespace()) == "go"


def _batch(**overrides) -> AnalyticalBatch:
    events = overrides.pop("events", (_event(),))
    values = dict(
        batch_id="batch",
        job_id="job",
        domain_name="domain",
        config_revision=1,
        logical_tick=1,
        logical_time=NOW,
        from_event_offset=0,
        to_event_offset=len(events),
        events=events,
    )
    values.update(overrides)
    return AnalyticalBatch(**values)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"batch_id": ""}, "identity cannot be empty"),
        ({"job_id": ""}, "identity cannot be empty"),
        ({"domain_name": ""}, "identity cannot be empty"),
        ({"from_event_offset": -1}, "from_event_offset must be >= 0"),
        (
            {"from_event_offset": 2, "to_event_offset": 1, "events": ()},
            "invalid analytical batch event range",
        ),
        (
            {"from_event_offset": 0, "to_event_offset": 2},
            "range does not match event count",
        ),
    ],
)
def test_analytical_batch_validates_contract(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _batch(**kwargs)
    assert _batch().to_event_offset == 1


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: SinkCheckpoint("", "sink"),
            "checkpoint identity cannot be empty",
        ),
        (
            lambda: SinkCheckpoint("job", ""),
            "checkpoint identity cannot be empty",
        ),
        (
            lambda: SinkCheckpoint("job", "sink", event_offset=-1),
            "event_offset must be >= 0",
        ),
        (
            lambda: SinkDelivery("", "sink", _batch()),
            "delivery identity cannot be empty",
        ),
        (
            lambda: SinkDelivery("delivery", "", _batch()),
            "delivery identity cannot be empty",
        ),
        (
            lambda: SinkDelivery("delivery", "sink", _batch(), status="bad"),
            "unsupported sink delivery status",
        ),
        (
            lambda: SinkDelivery("delivery", "sink", _batch(), attempts=-1),
            "attempts must be >= 0",
        ),
        (
            lambda: SinkBinding("", SimpleNamespace()),
            "binding name cannot be empty",
        ),
    ],
)
def test_sink_models_validate_contract(factory, message):
    with pytest.raises(ValueError, match=message):
        factory()


@dataclass
class _FakeState:
    id: str
    transitions: tuple = ()
    states: tuple = ()


@dataclass
class _FakeTransition:
    source: object
    targets: tuple
    events: tuple = ()
    internal: bool = False
    initial: bool = False


def test_statechart_topology_extracts_nested_eventful_and_eventless_edges():
    child = _FakeState("child")
    parent = _FakeState("parent", states=(child,))
    target = _FakeState("target")
    shared = _FakeTransition(
        source=parent,
        targets=(target,),
        events=(SimpleNamespace(id="go"), "fallback"),
        internal=True,
    )
    eventless = _FakeTransition(
        source=child,
        targets=(target,),
        events=(),
        initial=True,
    )
    parent.transitions = (shared,)
    child.transitions = (shared, eventless)

    class Chart:
        states = (parent, target)

    graph = graph_from_statechart(Chart())
    assert len(graph.edges) == 3
    assert {edge.event for edge in graph.edges} == {"go", "fallback", None}
    assert sum(edge.metadata["internal"] for edge in graph.edges) == 2
    assert sum(edge.metadata["initial"] for edge in graph.edges) == 1


def test_statechart_topology_validation_and_policy_resolution():
    with pytest.raises(TypeError, match="states collection"):
        graph_from_statechart(SimpleNamespace())

    class EmptyChart:
        states = ()

    with pytest.raises(ValueError, match="no transitions"):
        graph_from_statechart(EmptyChart())

    with pytest.raises(TypeError, match="state without id"):
        _state_id(object())
    assert _event_id(SimpleNamespace(id="go")) == "go"
    assert _event_id("raw") == "raw"

    class DefaultChart:
        pass

    assert isinstance(policy_from_statechart(DefaultChart()), TransitionPolicy)

    class ConfiguredChart:
        sose_transition_policy = TransitionPolicy({"go": 1})

    assert policy_from_statechart(ConfiguredChart()) is ConfiguredChart.sose_transition_policy

    class BadChart:
        sose_transition_policy = object()

    with pytest.raises(TypeError, match="must be a TransitionPolicy"):
        policy_from_statechart(BadChart())

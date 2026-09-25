from __future__ import annotations

from dataclasses import dataclass, field

from sose.factories.command import CommandFactory
from sose.factories.entity import EntityFactory
from sose.factories.event import EventFactory
from sose.factories.schedule import ScheduleFactory
from sose.factories.statechart import StateChartFactory
from sose.probability.runtime import ProbabilisticTransitionRuntime

from .clock import SimulationClock
from .events import DomainEvent
from .randomness import RandomSource
from .scheduler import Scheduler


@dataclass(slots=True)
class SimulationContext:
    clock: SimulationClock
    random: RandomSource
    scheduler: Scheduler
    pending_events: list[DomainEvent] = field(default_factory=list)
    entities: EntityFactory = field(init=False)
    commands: CommandFactory = field(init=False)
    events: EventFactory = field(init=False)
    schedules: ScheduleFactory = field(init=False)
    transitions: ProbabilisticTransitionRuntime = field(init=False)
    statecharts: StateChartFactory | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.entities = EntityFactory(now=lambda: self.clock.now)
        self.commands = CommandFactory(
            now=lambda: self.clock.now,
            tick=lambda: self.clock.tick,
        )
        self.events = EventFactory(
            now=lambda: self.clock.now,
            tick=lambda: self.clock.tick,
        )
        self.schedules = ScheduleFactory(
            now=lambda: self.clock.now,
            scheduler=self.scheduler,
        )
        self.transitions = ProbabilisticTransitionRuntime(
            random_source=self.random,
            tick=lambda: self.clock.tick,
        )

    def bind_statecharts(self, registry) -> None:
        self.statecharts = StateChartFactory(
            registry=registry,
            events=self.events,
            emit=self.emit,
            now=lambda: self.clock.now,
            transition_runtime=self.transitions,
            context=lambda: self,
        )

    def emit(self, event: DomainEvent) -> None:
        self.pending_events.append(event)

    def drain_events(self) -> list[DomainEvent]:
        events = self.pending_events
        self.pending_events = []
        return events

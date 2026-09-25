from __future__ import annotations

from collections.abc import Callable, Iterable

from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry
from sose.persistence.base import Persistence
from sose.scenarios.model import Scenario
from sose.scenarios.rules import ScenarioRule

from .context import SimulationContext
from .durable import DurableScheduledItem
from .events import Command
from .runtime import SimulationPosition


class Engine:
    """Deterministic logical-time simulation kernel."""

    def __init__(
        self,
        *,
        context: SimulationContext,
        registry: DomainRegistry,
        persistence: Persistence,
        rules_for: Callable[[str], Iterable[ScenarioRule]] | None = None,
        scenarios: Iterable[Scenario] = (),
    ) -> None:
        self.context = context
        self.registry = registry
        self.persistence = persistence
        self.rules_for = rules_for or (lambda _: ())
        self.context.scenarios.register_many(scenarios)
        self.context.bind_statecharts(registry)

    def dispatch(self, command: Command) -> None:
        with self.persistence.transaction() as uow:
            entity = uow.get_entity(command.entity_type, command.entity_id)
            if entity is None:
                raise KeyError(f"entity not found: {command.entity_type}/{command.entity_id}")

            if self.context.statecharts is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("statechart factory is not configured")

            chart = self.context.statecharts.bind(entity, caused_by=command)
            chart.send(command.name, **dict(command.payload))
            uow.save_entity(entity)
            emitted = self.context.drain_events()
            for event in emitted:
                uow.append_event(event)

        for event in emitted:
            self.context.scenarios.on_event(event)


    def dispatch_scheduled(self, item: DurableScheduledItem) -> None:
        """Execute and consume durable scheduled work in one commit boundary."""

        command = item.command
        work = item.work
        if command.command_id != work.command_id:
            raise ValueError("scheduled work does not match command")
        if work.due_at < self.context.clock.now:
            raise ValueError("scheduled work cannot execute before current logical time")
        self.context.clock.now = work.due_at

        with self.persistence.transaction() as uow:
            entity = uow.get_entity(command.entity_type, command.entity_id)
            if entity is None:
                raise KeyError(f"entity not found: {command.entity_type}/{command.entity_id}")

            if self.context.statecharts is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("statechart factory is not configured")

            chart = self.context.statecharts.bind(entity, caused_by=command)
            chart.send(command.name, **dict(command.payload))
            uow.save_entity(entity)
            emitted = self.context.drain_events()
            for event in emitted:
                uow.append_event(event)

            uow.delete_scheduled_work(work.work_id)
            uow.delete_command(command.command_id)

            previous = self.persistence.simulation_position()
            next_sequence = 1 if previous is None else previous.execution_sequence + 1
            uow.set_simulation_position(
                SimulationPosition(
                    logical_time=self.context.clock.now,
                    execution_sequence=next_sequence,
                    committed_sequence=next_sequence,
                )
            )

        for event in emitted:
            self.context.scenarios.on_event(event)


    def choose_transition(
        self,
        entity: Entity,
        *,
        event_kwargs: dict | None = None,
        scope: tuple[object, ...] = (),
    ):
        """Sample one currently enabled StateChart event without executing it."""

        if self.context.statecharts is None:  # pragma: no cover - constructor invariant
            raise RuntimeError("statechart factory is not configured")
        return self.context.statecharts.decide(
            entity,
            guard_kwargs=event_kwargs,
            scope=scope,
        )

    def dispatch_probabilistic(
        self,
        entity: Entity,
        *,
        event_kwargs: dict | None = None,
        scope: tuple[object, ...] = (),
        caused_by=None,
    ):
        """Sample and execute one enabled probabilistic StateChart event.

        The same event kwargs used while evaluating StateChart guards are forwarded
        to the selected event so the legality check and execution see one context.
        """

        decision = self.choose_transition(
            entity,
            event_kwargs=event_kwargs,
            scope=scope,
        )
        command = self.context.commands.create(
            decision.event,
            target=entity,
            caused_by=caused_by,
            payload=event_kwargs or {},
            key=(
                "probabilistic-transition",
                self.context.clock.tick,
                entity.entity_type,
                entity.id,
                *decision.configuration.states,
                *scope,
                decision.event,
            ),
        )
        self.dispatch(command)
        return decision

    def evaluate_entity(self, entity: Entity) -> None:
        """Evaluate scenario policies and dispatch resulting commands.

        Scenario rules choose valid intentions; they never mutate entity state directly.
        """
        for rule in self.rules_for(entity.entity_type):
            command_name = rule.evaluate(entity, self.context)
            if command_name:
                self.dispatch(
                    self.context.commands.create(
                        command_name,
                        target=entity,
                        due_at=self.context.clock.now,
                        key=(
                            "scenario",
                            self.context.clock.tick,
                            rule.name,
                            entity.entity_type,
                            entity.id,
                        ),
                    )
                )

    def advance_tick(self) -> None:
        """Evaluate external conditions, process due work, then commit the tick."""

        self.context.scenarios.on_tick()

        for command in self.context.scheduler.due(self.context.clock.now):
            self.dispatch(command)

        with self.persistence.transaction() as uow:
            uow.set_committed_tick(self.context.clock.tick)

        self.context.clock.advance()

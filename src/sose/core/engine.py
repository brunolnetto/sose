from __future__ import annotations

from collections.abc import Callable, Iterable

from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry
from sose.persistence.base import Persistence
from sose.scenarios.model import Scenario
from sose.scenarios.rules import ScenarioRule

from .context import SimulationContext
from .durable import DurableScheduledItem, RuntimeRebuilder
from .events import Command
from .resources import DurableResourceManager
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
        self.resources = DurableResourceManager(persistence)
        self.context.scenarios.register_many(scenarios)
        self.context.bind_statecharts(registry)

    def dispatch(self, command: Command) -> None:
        scenario_before = self.context.scenarios.snapshot_state()
        try:
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
                    self.context.scenarios.on_event(event)
                uow.set_scenario_state(self.context.scenarios.snapshot_state())
        except Exception:
            self.context.scenarios.restore_state(scenario_before)
            raise


    def dispatch_scheduled(self, item: DurableScheduledItem) -> bool:
        """Execute and consume durable scheduled work in one commit boundary.

        Returns False for an already-consumed stale callback so backend duplication
        cannot replay a committed transition.
        """

        command = item.command
        work = item.work
        if command.command_id != work.command_id:
            raise ValueError("scheduled work does not match command")
        if work.due_at != command.due_at:
            raise ValueError("scheduled work due time does not match command")
        previous_time = self.context.clock.now
        previous_tick = self.context.clock.tick
        try:
            with self.persistence.transaction() as uow:
                persisted_work = uow.get_scheduled_work(work.work_id)
                persisted_command = uow.get_command(command.command_id)
                if persisted_work != work or persisted_command != command:
                    return False
                if work.due_at < self.context.clock.now:
                    raise ValueError("scheduled work cannot execute before current logical time")

                self.context.clock.now = work.due_at
                entity = uow.get_entity(command.entity_type, command.entity_id)
                if entity is None:
                    raise KeyError(f"entity not found: {command.entity_type}/{command.entity_id}")

                if self.context.statecharts is None:  # pragma: no cover - constructor invariant
                    raise RuntimeError("statechart factory is not configured")

                chart = self.context.statecharts.bind(entity, caused_by=command)
                chart.send(command.name, **dict(command.payload))
                uow.save_entity(entity)
                emitted = self.context.drain_events()
                scenario_before = self.context.scenarios.snapshot_state()
                for event in emitted:
                    uow.append_event(event)
                    self.context.scenarios.on_event(event)
                uow.set_scenario_state(self.context.scenarios.snapshot_state())

                uow.delete_scheduled_work(work.work_id)
                uow.delete_command(command.command_id)

                previous = self.persistence.simulation_position()
                next_sequence = 1 if previous is None else previous.execution_sequence + 1
                uow.set_simulation_position(
                    SimulationPosition(
                        logical_time=self.context.clock.now,
                        execution_sequence=next_sequence,
                        committed_sequence=next_sequence,
                        logical_tick=self.context.clock.tick,
                    )
                )
        except Exception:
            self.context.clock.now = previous_time
            self.context.clock.tick = previous_tick
            if "scenario_before" in locals():
                self.context.scenarios.restore_state(scenario_before)
            raise

        return True


    def rebuild_backend(self, backend) -> int:
        """Restore the durable recovery position and rebuild pending backend work."""

        position = self.persistence.simulation_position()
        if position is not None:
            self.context.clock.now = position.logical_time
            self.context.clock.tick = position.logical_tick
        self.context.scenarios.restore_state(self.persistence.scenario_state())
        self.resources.rebuild_backend(backend)

        return RuntimeRebuilder(self.persistence).rebuild(
            backend,
            on_due=self.dispatch_scheduled,
        )


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

        scenario_before = self.context.scenarios.snapshot_state()
        try:
            self.context.scenarios.on_tick()
            with self.persistence.transaction() as uow:
                uow.set_scenario_state(self.context.scenarios.snapshot_state())
        except Exception:
            self.context.scenarios.restore_state(scenario_before)
            raise

        for command in self.context.scheduler.due(self.context.clock.now):
            self.dispatch(command)

        next_time = self.context.clock.now + self.context.clock.step
        next_tick = self.context.clock.tick + 1
        position = self.persistence.simulation_position()
        execution_sequence = 0 if position is None else position.execution_sequence
        committed_sequence = 0 if position is None else position.committed_sequence

        with self.persistence.transaction() as uow:
            uow.set_committed_tick(self.context.clock.tick)
            uow.set_simulation_position(
                SimulationPosition(
                    logical_time=next_time,
                    execution_sequence=execution_sequence,
                    committed_sequence=committed_sequence,
                    logical_tick=next_tick,
                )
            )

        self.context.clock.advance()

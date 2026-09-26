from __future__ import annotations

from collections.abc import Callable, Iterable

from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry
from sose.persistence.base import Persistence
from sose.scenarios.model import Scenario
from sose.scenarios.rules import ScenarioRule

from .context import SimulationContext
from .durable import DurableScheduledItem, DurableScheduler, RuntimeRebuilder
from .events import Command
from .resources import DurableResourceManager
from .stores import DurableStoreManager
from .preemption import DurablePreemptiveResourceManager
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
        store_filters=None,
    ) -> None:
        self.context = context
        self.registry = registry
        self.persistence = persistence
        self.rules_for = rules_for or (lambda _: ())
        self.resources = DurableResourceManager(persistence)
        self.preemptive_resources = DurablePreemptiveResourceManager(persistence)
        self.stores = DurableStoreManager(persistence, filters=store_filters)
        self.scheduler = DurableScheduler(persistence)
        self.context.schedules.bind_scheduler(self.scheduler)
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


    def _dispatch_scheduled_in_uow(self, item: DurableScheduledItem, uow) -> bool:
        """Execute one durable item inside an existing transaction."""

        command = item.command
        work = item.work
        if command.command_id != work.command_id:
            raise ValueError("scheduled work does not match command")
        if work.due_at != command.due_at:
            raise ValueError("scheduled work due time does not match command")

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
        for event in emitted:
            uow.append_event(event)
            self.context.scenarios.on_event(event)
        uow.set_scenario_state(self.context.scenarios.snapshot_state())

        uow.delete_scheduled_work(work.work_id)
        uow.delete_command(command.command_id)
        return True

    def dispatch_scheduled(self, item: DurableScheduledItem) -> bool:
        """Execute and consume one durable callback atomically.

        Returns False for an already-consumed stale callback so backend duplication
        cannot replay a committed transition.
        """

        previous_time = self.context.clock.now
        previous_tick = self.context.clock.tick
        scenario_before = self.context.scenarios.snapshot_state()
        pending_before = list(self.context.pending_events)
        try:
            with self.persistence.transaction() as uow:
                if not self._dispatch_scheduled_in_uow(item, uow):
                    return False

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
            self.context.pending_events = pending_before
            self.context.scenarios.restore_state(scenario_before)
            raise

        return True


    def rebuild_backend(self, backend) -> int:
        """Restore durable runtime state and attach the fresh live backend."""

        self.scheduler.detach_backend()
        rebuilt = RuntimeRebuilder(
            self.persistence,
            context=self.context,
            resources=self.resources,
            stores=self.stores,
            preemptive_resources=self.preemptive_resources,
        ).rebuild(
            backend,
            on_due=self.dispatch_scheduled,
        )
        self.scheduler.attach_backend(
            backend,
            on_due=self.dispatch_scheduled,
        )
        return rebuilt


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
        """Evaluate and atomically commit one fixed logical-time interval."""

        tick_start = self.context.clock.now
        tick_end = tick_start + self.context.clock.step
        current_tick = self.context.clock.tick
        next_tick = current_tick + 1

        scenario_before = self.context.scenarios.snapshot_state()
        pending_before = list(self.context.pending_events)
        position_before = self.persistence.simulation_position()
        execution_sequence = (
            0 if position_before is None else position_before.execution_sequence
        )
        committed_sequence = (
            0 if position_before is None else position_before.committed_sequence
        )

        try:
            self.context.scenarios.on_tick()

            # The legacy in-memory scheduler is retained only as a compatibility
            # bridge. Engine-bound ScheduleFactory instances use DurableScheduler.
            for command in self.context.scheduler.due(tick_start):
                self.dispatch(command)

            with self.persistence.transaction() as uow:
                uow.set_scenario_state(self.context.scenarios.snapshot_state())

                executed = 0
                for item in self.scheduler.due(tick_end):
                    if self._dispatch_scheduled_in_uow(item, uow):
                        executed += 1

                next_sequence = execution_sequence + executed
                uow.set_committed_tick(current_tick)
                uow.set_simulation_position(
                    SimulationPosition(
                        logical_time=tick_end,
                        execution_sequence=next_sequence,
                        committed_sequence=next_sequence,
                        logical_tick=next_tick,
                    )
                )
        except Exception:
            self.context.clock.now = tick_start
            self.context.clock.tick = current_tick
            self.context.pending_events = pending_before
            self.context.scenarios.restore_state(scenario_before)
            raise

        self.context.clock.now = tick_end
        self.context.clock.tick = next_tick

"""Refresh-before-read query overrides of PostgresPersistence (split from postgres.py)."""

from __future__ import annotations

from .memory import MemoryPersistence


class PostgresFreshReadsMixin(MemoryPersistence):
    """Refresh the local record cache from PostgreSQL before every read query.

    The concrete adapter provides ``_refresh_from_db``.
    """

    def _fresh(self) -> None:
        self._refresh_from_db()

    def job_state(self, job_id: str):
        self._fresh()
        return super().job_state(job_id)

    def domain_delivery(self, mutation_id: str):
        self._fresh()
        return super().domain_delivery(mutation_id)

    def domain_deliveries(self):
        self._fresh()
        return super().domain_deliveries()

    def sink_delivery(self, delivery_id: str):
        self._fresh()
        return super().sink_delivery(delivery_id)

    def sink_deliveries(self, *, job_id=None, sink_name=None):
        self._fresh()
        return super().sink_deliveries(job_id=job_id, sink_name=sink_name)

    def sink_checkpoint(self, job_id: str, sink_name: str):
        self._fresh()
        return super().sink_checkpoint(job_id, sink_name)

    def sink_checkpoints(self, *, job_id=None):
        self._fresh()
        return super().sink_checkpoints(job_id=job_id)

    def business_effect(self, effect_id: str):
        self._fresh()
        return super().business_effect(effect_id)

    def business_effects(self):
        self._fresh()
        return super().business_effects()

    def job_states(self):
        self._fresh()
        return super().job_states()

    def committed_tick(self, *, owner_id: str) -> int:
        self._fresh()
        return super().committed_tick(owner_id=owner_id)

    def events(self):
        self._fresh()
        return super().events()

    def count_events(self, *, owner_id: str) -> int:
        self._fresh()
        return super().count_events(owner_id=owner_id)

    def entity(self, entity_type: str, entity_id: str):
        self._fresh()
        return super().entity(entity_type, entity_id)

    def command(self, command_id: str):
        self._fresh()
        return super().command(command_id)

    def missing_command_ids(self, command_ids):
        self._fresh()
        return super().missing_command_ids(command_ids)

    def scheduled_work(self):
        self._fresh()
        return super().scheduled_work()

    def due_scheduled_work(self, at):
        self._fresh()
        return tuple(work for work in super().scheduled_work() if work.due_at <= at)

    def simulation_position(self, *, owner_id: str):
        self._fresh()
        return super().simulation_position(owner_id=owner_id)

    def scenario_state(self, *, owner_id: str):
        self._fresh()
        return super().scenario_state(owner_id=owner_id)

    def resource_definitions(self):
        self._fresh()
        return super().resource_definitions()

    def resource_demands(self):
        self._fresh()
        return super().resource_demands()

    def resource_reservations(self):
        self._fresh()
        return super().resource_reservations()

    def resource_release_intents(self):
        self._fresh()
        return super().resource_release_intents()

    def store_definitions(self):
        self._fresh()
        return super().store_definitions()

    def store_items(self):
        self._fresh()
        return super().store_items()

    def store_put_intents(self):
        self._fresh()
        return super().store_put_intents()

    def store_get_requests(self):
        self._fresh()
        return super().store_get_requests()

    def store_get_results(self):
        self._fresh()
        return super().store_get_results()

    def container_definitions(self):
        self._fresh()
        return super().container_definitions()

    def container_states(self):
        self._fresh()
        return super().container_states()

    def container_operation_intents(self):
        self._fresh()
        return super().container_operation_intents()

    def container_operation_results(self):
        self._fresh()
        return super().container_operation_results()

    def preemptive_resource_definitions(self):
        self._fresh()
        return super().preemptive_resource_definitions()

    def preemptive_resource_demands(self):
        self._fresh()
        return super().preemptive_resource_demands()

    def preemptive_resource_reservations(self):
        self._fresh()
        return super().preemptive_resource_reservations()

    def preemptive_resource_release_intents(self):
        self._fresh()
        return super().preemptive_resource_release_intents()

    def resource_preemption_results(self):
        self._fresh()
        return super().resource_preemption_results()

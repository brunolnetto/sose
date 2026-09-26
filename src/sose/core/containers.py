from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sose.backends.base import ContainerRequest
from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
)
from sose.persistence.base import Persistence


class DurableContainerManager:
    """Durable quantitative state reconstructed into an ephemeral Container backend."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    def define(self, definition: ContainerDefinition) -> None:
        state = ContainerState(definition.name, level=definition.initial)
        with self._persistence.transaction() as uow:
            uow.save_container_definition(definition)
            existing = uow.get_container_state(definition.name)
            if existing is not None and existing != state:
                raise ValueError(
                    f"container state already exists: {definition.name}"
                )
            uow.save_container_state(state)

    def validate_rebuild(self) -> None:
        definitions = {
            definition.name: definition
            for definition in self._persistence.container_definitions()
        }
        states = {
            state.name: state
            for state in self._persistence.container_states()
        }

        for name, definition in definitions.items():
            state = states.get(name)
            if state is None:
                raise RuntimeError(f"container {name} is missing durable state")
            if state.level > definition.capacity:
                raise RuntimeError(f"container {name} level exceeds capacity")

        for name in states:
            if name not in definitions:
                raise RuntimeError(f"container state references unknown definition: {name}")

        results = {
            result.request_id: result
            for result in self._persistence.container_operation_results()
        }
        for intent in self._persistence.container_operation_intents():
            if intent.container_name not in definitions:
                raise RuntimeError(
                    f"container operation references unknown definition: {intent.request_id}"
                )
            if intent.request_id in results:
                raise RuntimeError(
                    f"container request is both pending and completed: {intent.request_id}"
                )

        for result in results.values():
            definition = definitions.get(result.container_name)
            if definition is None:
                raise RuntimeError(
                    f"container result references unknown definition: {result.request_id}"
                )
            expected = (
                result.level_before + result.amount
                if result.operation == "put"
                else result.level_before - result.amount
            )
            if result.level_after != expected:
                raise RuntimeError(
                    f"container result has inconsistent level delta: {result.request_id}"
                )
            if result.level_after < 0 or result.level_after > definition.capacity:
                raise RuntimeError(
                    f"container result level is outside capacity: {result.request_id}"
                )

    def rebuild_backend(self, backend) -> int:
        self.validate_rebuild()
        definitions = {
            definition.name: definition
            for definition in self._persistence.container_definitions()
        }
        states = {
            state.name: state
            for state in self._persistence.container_states()
        }

        for definition in definitions.values():
            backend.create_container(
                definition.name,
                capacity=definition.capacity,
                initial=states[definition.name].level,
            )

        intents = sorted(
            self._persistence.container_operation_intents(),
            key=lambda intent: (intent.sequence, intent.request_id),
        )
        for intent in intents:
            self._submit_intent(backend, intent)
        return len(definitions) + len(intents)

    def put(
        self,
        backend,
        *,
        container_name: str,
        request_id: str,
        amount: float,
        requested_at: datetime,
        on_completed: Callable[[ContainerOperationResult], None] | None = None,
    ) -> ContainerOperationIntent:
        return self._request(
            backend,
            container_name=container_name,
            request_id=request_id,
            operation="put",
            amount=amount,
            requested_at=requested_at,
            on_completed=on_completed,
        )

    def get(
        self,
        backend,
        *,
        container_name: str,
        request_id: str,
        amount: float,
        requested_at: datetime,
        on_completed: Callable[[ContainerOperationResult], None] | None = None,
    ) -> ContainerOperationIntent:
        return self._request(
            backend,
            container_name=container_name,
            request_id=request_id,
            operation="get",
            amount=amount,
            requested_at=requested_at,
            on_completed=on_completed,
        )

    def result(self, request_id: str) -> ContainerOperationResult | None:
        return next(
            (
                result
                for result in self._persistence.container_operation_results()
                if result.request_id == request_id
            ),
            None,
        )

    def commit(
        self,
        request_id: str,
        *,
        completed_at: datetime,
    ) -> ContainerOperationResult:
        existing = self.result(request_id)
        if existing is not None:
            return existing

        intent = next(
            (
                intent
                for intent in self._persistence.container_operation_intents()
                if intent.request_id == request_id
            ),
            None,
        )
        if intent is None:
            raise KeyError(f"unknown container operation intent: {request_id}")

        state = next(
            (
                state
                for state in self._persistence.container_states()
                if state.name == intent.container_name
            ),
            None,
        )
        if state is None:
            raise KeyError(f"unknown durable container state: {intent.container_name}")
        definition = self._definition(intent.container_name)

        level_after = (
            state.level + intent.amount
            if intent.operation == "put"
            else state.level - intent.amount
        )
        if level_after < 0 or level_after > definition.capacity:
            raise RuntimeError(
                f"backend completed infeasible container operation: {request_id}"
            )

        result = ContainerOperationResult(
            request_id=intent.request_id,
            container_name=intent.container_name,
            operation=intent.operation,
            amount=intent.amount,
            completed_at=completed_at,
            level_before=state.level,
            level_after=level_after,
            sequence=intent.sequence,
        )
        next_state = ContainerState(intent.container_name, level=level_after)

        with self._persistence.transaction() as uow:
            persisted_intent = uow.get_container_operation_intent(request_id)
            if persisted_intent != intent:
                completed = uow.get_container_operation_result(request_id)
                if completed is not None:
                    return completed
                raise RuntimeError(f"container operation intent changed: {request_id}")

            persisted_state = uow.get_container_state(intent.container_name)
            if persisted_state != state:
                raise RuntimeError(
                    f"container state changed before operation commit: {request_id}"
                )
            uow.save_container_state(next_state)
            uow.save_container_operation_result(result)
            uow.delete_container_operation_intent(request_id)
        return result

    def _request(
        self,
        backend,
        *,
        container_name: str,
        request_id: str,
        operation: str,
        amount: float,
        requested_at: datetime,
        on_completed: Callable[[ContainerOperationResult], None] | None,
    ) -> ContainerOperationIntent:
        self._definition(container_name)
        if any(
            intent.request_id == request_id
            for intent in self._persistence.container_operation_intents()
        ) or any(
            result.request_id == request_id
            for result in self._persistence.container_operation_results()
        ):
            raise ValueError(f"container request already exists: {request_id}")

        intent = ContainerOperationIntent(
            request_id=request_id,
            container_name=container_name,
            operation=operation,
            amount=amount,
            requested_at=requested_at,
            sequence=self._next_sequence(),
        )
        with self._persistence.transaction() as uow:
            uow.save_container_operation_intent(intent)

        self._submit_intent(backend, intent, on_completed=on_completed)
        return intent

    def _submit_intent(
        self,
        backend,
        intent: ContainerOperationIntent,
        *,
        on_completed: Callable[[ContainerOperationResult], None] | None = None,
    ) -> None:
        def completed(_: ContainerRequest) -> None:
            result = self.commit(intent.request_id, completed_at=backend.now)
            if on_completed is not None:
                on_completed(result)

        if intent.operation == "put":
            backend.put_container(
                intent.container_name,
                request_id=intent.request_id,
                amount=intent.amount,
                on_completed=completed,
            )
        else:
            backend.get_container(
                intent.container_name,
                request_id=intent.request_id,
                amount=intent.amount,
                on_completed=completed,
            )

    def _definition(self, name: str) -> ContainerDefinition:
        definition = next(
            (
                definition
                for definition in self._persistence.container_definitions()
                if definition.name == name
            ),
            None,
        )
        if definition is None:
            raise KeyError(f"unknown container definition: {name}")
        return definition

    def _next_sequence(self) -> int:
        return max(
            [
                *(
                    intent.sequence
                    for intent in self._persistence.container_operation_intents()
                ),
                *(
                    result.sequence
                    for result in self._persistence.container_operation_results()
                ),
            ],
            default=0,
        ) + 1

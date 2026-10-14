"""Deterministic same-owner stale-read fencing using two PostgreSQL connections.

Worker A must not commit effects calculated from position P0 after worker B
has atomically moved the very same owner's boundary to P1. An event barrier
forces the dangerous ordering; retries or scheduling luck cannot make it pass.
"""
from __future__ import annotations

import os
from threading import Event, Thread
from uuid import uuid4

import pytest

from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
)
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL required")


def test_same_owner_stale_tick_cannot_overwrite_position_or_repeat_effects():
    assert DSN is not None
    namespace = "owner_cas_" + uuid4().hex[:12]
    owner_id = "tutorial-job-cas-fencing"
    a_read_p0 = Event()
    release_a = Event()

    class PausedBeforeOwnerAdmission(PostgresPersistence):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.paused_once = False

        def simulation_position(self, *, owner_id: str):
            position = super().simulation_position(owner_id=owner_id)
            if not self.paused_once:
                self.paused_once = True
                a_read_p0.set()
                if not release_a.wait(timeout=15):
                    raise TimeoutError("worker A was not released after P0 read")
            return position

    with PostgresPersistence(DSN, namespace=namespace) as initial:
        job = seed_job(initial, owner_id=owner_id, job_key="cas-fencing")
        context, engine = build_runtime(initial, owner_id=owner_id)
        command = context.commands.create(
            "start",
            target=job,
            due_at=ORIGIN,
            key=("cas-fencing", owner_id, "start"),
        )
        scheduled = engine.scheduler.schedule(command)
        assert initial.simulation_position(owner_id=owner_id) is None

    with (
        PausedBeforeOwnerAdmission(DSN, namespace=namespace) as writer_a,
        PostgresPersistence(DSN, namespace=namespace) as writer_b,
    ):
        context_a, engine_a = build_runtime(writer_a, owner_id=owner_id)
        _, engine_b = build_runtime(writer_b, owner_id=owner_id)
        errors: list[Exception] = []

        def stale_worker() -> None:
            try:
                engine_a.advance_tick()
            except Exception as exc:
                errors.append(exc)

        thread = Thread(target=stale_worker, name="stale-owner-writer")
        thread.start()
        try:
            assert a_read_p0.wait(timeout=10), "worker A did not observe P0"
            assert writer_b.simulation_position(owner_id=owner_id) is None
            engine_b.advance_tick()
            position_p1 = writer_b.simulation_position(owner_id=owner_id)
            assert position_p1 is not None and position_p1.logical_tick == 1
        finally:
            release_a.set()
            thread.join(timeout=15)

        assert not thread.is_alive(), "stale worker did not finish"
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "stale owner simulation position" in str(errors[0])
        assert context_a.clock.now == ORIGIN
        assert context_a.clock.tick == 0

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        assert recovered.simulation_position(owner_id=owner_id) == position_p1
        assert recovered.committed_tick(owner_id=owner_id) == 0
        assert recovered.entity("tutorial_job", job.id).state == "running"
        assert recovered.command(command.command_id) is None
        assert all(item.work_id != scheduled.work_id for item in recovered.scheduled_work())
        owner_events = [
            event for event in recovered.events()
            if event.owner_id == owner_id and event.entity_id == job.id
        ]
        assert len(owner_events) == 1

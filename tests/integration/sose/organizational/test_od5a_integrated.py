"""TDD contracts for #490: real recurring SimPy source + durable boundary phases.

Compare the independent #488 reducer/reference orchestration with the new
fenced source-boundary adapter after EVERY durable occurrence, not just
terminal throughput. PostgreSQL cases run on actual separated namespaces.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import initialize_od5a_durable
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_recurring import initialize_od5a_recurring_job
from sose.organizational.od5a_reference_runner import advance_od5a_reference_occurrence
from sose.organizational.od5a_integrated import advance_od5a_integrated_occurrence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.postgres import PostgresPersistence


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
PHYSICAL = frozenset({"od5a.execution.v1", "od5a.coordination.v1"})
PHASES = ("published", "claimed", "acknowledged", "materialized", "certified")
DSN = os.getenv("SOSE_TEST_POSTGRES_DSN")


def _arm():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            key: EvidenceClass.ASSUMED for key in
            ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")
        },
    )
    return compile_od5a_arms(
        baseline=model, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            key: OD5aChargedCost(8, 1, 1, 1)
            for key in ("capacity_only", "topology_only",
                        "coordination_only", "structural_combined")
        },
    )[0]


def _items(job):
    return (OD5aWorkItem(
        "item:" + job, 0.0, (("a", 0.2), ("b", 0.3), ("c", 0.1)),
    ),)


def _truth(store, organizations):
    """Audit the physical/event DAG and every independent downstream boundary."""
    with store.transaction() as uow:
        events = tuple(sorted(
            (e for e in uow.events() if e.entity_id in organizations),
            key=lambda e: e.event_id,
        ))
        physical = tuple(e for e in events if e.name in PHYSICAL)
        deliveries = tuple(sorted(
            (d for d in uow.boundary_deliveries()
             if (m := uow.get_boundary_message(d.message_id)) is not None
             and m.correlation_id in organizations),
            key=lambda d: d.delivery_id,
        ))
        messages = tuple(uow.get_boundary_message(d.message_id) for d in deliveries)
        return (
            tuple(uow.get_entity("od5a_durable_pilot", j) for j in organizations),
            events,
            tuple(sorted(uow.resource_reservations(), key=lambda x: x.reservation_id)),
            messages, deliveries,
            tuple(uow.get_boundary_consumption(d.delivery_id) for d in deliveries),
            tuple(
                (m.message_id, uow.get_command(deterministic_id(
                    "od5a-physical-command", m.message_id,
                ))) for m in messages
            ),
            tuple(
                (e.event_id, uow.get_entity("od5a_physical_observation", e.event_id))
                for e in physical
            ),
            tuple(sorted(uow.business_effects(), key=lambda x: x.effect_id)),
        )


@pytest.mark.parametrize("adapter,organizations", [
    ("sqlite", ("org:A",)),
    ("sqlite", ("org:A", "org:B")),
    ("postgres", ("org:A",)),
])
def test_integrated_occurrences_match_reference_at_every_commit(
    tmp_path, adapter, organizations,
):
    if adapter == "postgres" and not DSN:
        pytest.skip("live PostgreSQL DSN required")
    with ExitStack() as stack:
        if adapter == "postgres":
            suffix = uuid4().hex[:12]
            oracle = stack.enter_context(PostgresPersistence(
                DSN, namespace="od5a_oracle_" + suffix,
            ))
            integrated = stack.enter_context(PostgresPersistence(
                DSN, namespace="od5a_integrated_" + suffix,
            ))
        else:
            oracle = stack.enter_context(SQLitePersistence(tmp_path / "oracle.sqlite"))
            integrated = stack.enter_context(SQLitePersistence(tmp_path / "integrated.sqlite"))
        arm = _arm()
        for job_id in organizations:
            initialize_od5a_durable(
                oracle, job_id=job_id, arm=arm, items=_items(job_id),
            )
            initialize_od5a_recurring_job(
                integrated, job_id=job_id, arm=arm, items=_items(job_id),
            )
        audit = {job_id: [] for job_id in organizations}
        completed = set()
        for _ in range(180):
            for job_id in organizations:
                if job_id in completed:
                    continue
                reference = advance_od5a_reference_occurrence(
                    oracle, job_id=job_id, owner_id="worker:" + job_id,
                    now=NOW, lease_duration=LEASE,
                )
                job = integrated.job_state(job_id)
                trigger = deterministic_id(
                    "od5a-recurring-tick", job_id, job.run_count + 1,
                )
                actual = advance_od5a_integrated_occurrence(
                    integrated, job_id=job_id, owner_id="worker:" + job_id,
                    now=NOW, lease_duration=LEASE,
                    source_trigger_id=trigger,
                )
                audit[job_id].append(actual.action)
                if reference.action == "complete":
                    assert actual.action in {"source_complete", "complete"}
                else:
                    assert actual.action == reference.action
                # The source has its own durable JobState, but all physical,
                # temporal, causal, transport and certification truth must
                # agree with the independently driven reference.
                assert _truth(integrated, organizations) == _truth(oracle, organizations)
                if actual.action == "complete":
                    completed.add(job_id)
            if len(completed) == len(organizations):
                break
        else:
            pytest.fail("integrated recurring adapter did not converge")
        for job_id in organizations:
            actions = audit[job_id]
            assert "source_advanced" in actions
            assert set(PHASES) <= set(actions)
            assert "complete" in actions
            with integrated.transaction() as uow:
                state = uow.get_job_state(job_id)
                source = uow.get_entity("od5a_durable_pilot", job_id)
                assert state.run_count == source.attributes["step"]
                assert state.next_tick == source.attributes["step"]
                assert state.active_trigger_id is None
            # Once every boundary phase is certified, a lost response for the
            # final persisted trigger MUST remain distinguishable from a
            # subsequent no-op completion probe.
            before_replay = _truth(integrated, organizations)
            last_trigger = integrated.job_state(job_id).last_completed_trigger_id
            assert last_trigger is not None
            replay = advance_od5a_integrated_occurrence(
                integrated, job_id=job_id, owner_id="recovered:" + job_id,
                now=NOW, lease_duration=LEASE, source_trigger_id=last_trigger,
            )
            assert replay.action == "source_replayed"
            assert replay.source_trigger_id == last_trigger
            future_trigger = deterministic_id(
                "od5a-recurring-tick", job_id, state.run_count + 1,
            )
            finished = advance_od5a_integrated_occurrence(
                integrated, job_id=job_id, owner_id="recovered:" + job_id,
                now=NOW, lease_duration=LEASE, source_trigger_id=future_trigger,
            )
            assert finished.action == "complete"
            assert _truth(integrated, organizations) == before_replay
            assert integrated.job_state(job_id) == state


def test_boundary_backlog_precedes_next_source_step(tmp_path):
    with SQLitePersistence(tmp_path / "integrated.sqlite") as store:
        job_id = "org:backlog"
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arm(), items=_items(job_id),
        )
        for _ in range(35):
            current = store.job_state(job_id)
            trigger = deterministic_id(
                "od5a-recurring-tick", job_id, current.run_count + 1,
            )
            step = advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="one",
                now=NOW, lease_duration=LEASE, source_trigger_id=trigger,
            )
            if step.action == "published":
                tick = store.job_state(job_id).run_count
                later = advance_od5a_integrated_occurrence(
                    store, job_id=job_id, owner_id="one",
                    now=NOW, lease_duration=LEASE,
                    source_trigger_id=trigger,
                )
                assert later.action == "claimed"
                assert store.job_state(job_id).run_count == tick
                return
        pytest.fail("no physical publication reached after source steps")


def test_terminal_source_checkpoint_restarts_without_phantom_step(tmp_path):
    """The final physical action commits done and run_count in the SAME UoW."""
    from sose.organizational.od5a_recurring import run_od5a_recurring_tick

    path = tmp_path / "terminal.sqlite"
    job_id = "org:terminal"
    with SQLitePersistence(path) as store:
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arm(), items=_items(job_id),
        )
        for _ in range(100):
            job = store.job_state(job_id)
            completed_trigger_id = deterministic_id(
                "od5a-recurring-tick", job_id, job.run_count + 1,
            )
            result = run_od5a_recurring_tick(
                store, job_id=job_id, owner_id="worker",
                now=NOW, lease_duration=LEASE,
                trigger_id=completed_trigger_id,
            )
            with store.transaction() as uow:
                source = uow.get_entity("od5a_durable_pilot", job_id)
            # The terminal source is *already* marked done by the last actual
            # physical event, so the final committed callback says advanced.
            assert result.action == "advanced"
            if source.attributes["done"]:
                break
        else:
            pytest.fail("source did not finish its physical DAG")
        job = store.job_state(job_id)
        assert job.run_count == source.attributes["step"]
        assert job.next_tick == source.attributes["step"]
        assert job.active_trigger_id is None
        assert job.last_completed_trigger_id == completed_trigger_id
        before = _truth(store, (job_id,))

    # Reconstruct solely from persisted truth, not live SimPy or prior handles.
    with SQLitePersistence(path) as recovered:
        assert _truth(recovered, (job_id,)) == before
        replay = run_od5a_recurring_tick(
            recovered, job_id=job_id, owner_id="other",
            now=NOW, lease_duration=LEASE,
            trigger_id=completed_trigger_id,
        )
        assert replay.action == "replayed"
        future_trigger_id = deterministic_id(
            "od5a-recurring-tick", job_id, job.run_count + 1,
        )
        for _ in range(2):
            completed = run_od5a_recurring_tick(
                recovered, job_id=job_id, owner_id="other",
                now=NOW, lease_duration=LEASE,
                trigger_id=future_trigger_id,
            )
            assert completed.action == "complete"
        assert _truth(recovered, (job_id,)) == before
        assert recovered.job_state(job_id) == job


@pytest.mark.parametrize("adapter", ["sqlite", "postgres"])
@pytest.mark.parametrize("tampering", ["forged_trigger", "forged_owner"])
def test_integrated_reconciliation_refuses_malformed_released_source_fence(
    tmp_path, adapter, tampering,
):
    """Do not turn a corrupt source fence into an apparently valid ACK."""
    if adapter == "postgres" and not DSN:
        pytest.skip("live PostgreSQL DSN required")
    with (
        PostgresPersistence(
            DSN, namespace="od5a_lease_" + uuid4().hex[:12],
        ) if adapter == "postgres" else
        SQLitePersistence(tmp_path / "corrupt-lease.sqlite")
    ) as store:
        job_id = "org:lease"
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arm(), items=_items(job_id),
        )
        # Reach an already committed physical event with an actual pending
        # delivery. The next phase would normally be a durable claim.
        for _ in range(100):
            job = store.job_state(job_id)
            next_trigger = deterministic_id(
                "od5a-recurring-tick", job_id, job.run_count + 1,
            )
            step = advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="publisher",
                now=NOW, lease_duration=LEASE, source_trigger_id=next_trigger,
            )
            if step.action == "published":
                break
        else:
            pytest.fail("physical source never reached boundary publication")
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", job_id)
            assert lease is not None and lease.state == "released"
            if tampering == "forged_trigger":
                lease.attributes["trigger_id"] = "forged"
            else:
                lease.attributes["owner_id"] = "ghost"
            uow.save_entity(lease)
        before = _truth(store, (job_id,))
        prior_job = store.job_state(job_id)
        with pytest.raises(ValueError, match="released lease history"):
            advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="consumer",
                now=NOW, lease_duration=LEASE, source_trigger_id=next_trigger,
            )
        assert _truth(store, (job_id,)) == before
        assert store.job_state(job_id) == prior_job

@pytest.mark.parametrize("adapter", ["sqlite", "postgres"])
@pytest.mark.parametrize("tampering", [
    "missing_claim", "wrong_trigger", "missing_owner", "wrong_epoch",
    "invalid_expiry", "wrong_lifecycle",
])
def test_integrated_reconciliation_rejects_corrupt_active_source_fence(
    tmp_path, adapter, tampering,
):
    """A claimed but corrupt runner lease cannot authorize downstream ACK."""
    if adapter == "postgres" and not DSN:
        pytest.skip("live PostgreSQL DSN required")
    from sose.organizational.od5a_recurring import claim_od5a_recurring_tick

    with (
        PostgresPersistence(DSN, namespace="od5a_active_" + uuid4().hex[:12])
        if adapter == "postgres" else
        SQLitePersistence(tmp_path / "active-lease.sqlite")
    ) as store:
        job_id = "org:active"
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arm(), items=_items(job_id),
        )
        # A physical event and its publication must be independently durable
        # *before* the next physical source claim becomes active.
        for _ in range(80):
            job = store.job_state(job_id)
            trigger = deterministic_id(
                "od5a-recurring-tick", job_id, job.run_count + 1,
            )
            step = advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="worker",
                now=NOW, lease_duration=LEASE, source_trigger_id=trigger,
            )
            if step.action == "published":
                break
        else:
            pytest.fail("no physical event publication available to falsify")

        job = store.job_state(job_id)
        next_trigger = deterministic_id(
            "od5a-recurring-tick", job_id, job.run_count + 1,
        )
        claim = claim_od5a_recurring_tick(
            store, job_id=job_id, owner_id="worker",
            now=NOW, lease_duration=LEASE, trigger_id=next_trigger,
        )
        assert claim is not None
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", job_id)
            assert lease is not None and lease.state == "claimed"
            if tampering == "missing_claim":
                lease.state = "released"
            elif tampering == "wrong_trigger":
                lease.attributes["trigger_id"] = "fabricated"
            elif tampering == "missing_owner":
                lease.attributes["owner_id"] = None
            elif tampering == "wrong_epoch":
                lease.attributes["epoch"] = 0
            elif tampering == "invalid_expiry":
                lease.attributes["expires_at"] = lease.updated_at.isoformat()
            else:
                from dataclasses import replace
                active = uow.get_job_state(job_id)
                uow.save_job_state(replace(active, phase="idle"))
            uow.save_entity(lease)
        before = _truth(store, (job_id,))
        job_before = store.job_state(job_id)
        with pytest.raises(
            (ValueError, RuntimeError),
            match="active lease|runner claim|claimed lease|runner epoch",
        ):
            advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="reconciler",
                now=NOW, lease_duration=LEASE, source_trigger_id=next_trigger,
            )
        assert _truth(store, (job_id,)) == before
        assert store.job_state(job_id) == job_before


def test_postgres_integrated_boundary_uses_job_scoped_writer_epoch(tmp_path, monkeypatch):
    """No legacy exclusive namespace-wide boundary lock for integrated jobs."""
    if not DSN:
        pytest.skip("live PostgreSQL DSN required")
    from contextlib import contextmanager

    namespace = "od5a_scoped_" + uuid4().hex[:12]
    org = "org:scoped"
    captured = []
    original = PostgresPersistence.boundary_transaction

    @contextmanager
    def instrument(self, *, owner_epoch=None, writer_scope=None):
        captured.append((owner_epoch, writer_scope))
        with original(
            self, owner_epoch=owner_epoch, writer_scope=writer_scope,
        ) as uow:
            yield uow

    monkeypatch.setattr(PostgresPersistence, "boundary_transaction", instrument)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        for _ in range(40):
            job = store.job_state(org)
            trigger = deterministic_id(
                "od5a-recurring-tick", org, job.run_count + 1,
            )
            step = advance_od5a_integrated_occurrence(
                store, job_id=org, owner_id="worker",
                now=NOW, lease_duration=LEASE, source_trigger_id=trigger,
            )
            if step.action == "published":
                break
        else:
            pytest.fail("integrated source did not publish a physical event")
    assert captured
    assert all(epoch is not None and epoch > 0 for epoch, scope in captured)
    assert set(scope for _, scope in captured) == {"od5a:boundary:" + org}


@pytest.mark.parametrize("adapter", ["sqlite", "postgres"])
def test_terminal_source_rejects_forged_active_claim_before_publication(
    tmp_path, adapter,
):
    """Reject a mutually impossible terminal source + newly active lease."""
    if adapter == "postgres" and not DSN:
        pytest.skip("live PostgreSQL DSN required")
    from dataclasses import replace
    from sose.organizational.od5a_recurring import run_od5a_recurring_tick

    with (
        PostgresPersistence(DSN, namespace="od5a_done_" + uuid4().hex[:12])
        if adapter == "postgres" else
        SQLitePersistence(tmp_path / "terminal-forged.sqlite")
    ) as store:
        job_id = "org:terminal-forged"
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arm(), items=_items(job_id),
        )
        for _ in range(100):
            job = store.job_state(job_id)
            trigger = deterministic_id(
                "od5a-recurring-tick", job_id, job.run_count + 1,
            )
            result = run_od5a_recurring_tick(
                store, job_id=job_id, owner_id="worker",
                now=NOW, lease_duration=LEASE, trigger_id=trigger,
            )
            assert result.action == "advanced"
            with store.transaction() as uow:
                source = uow.get_entity("od5a_durable_pilot", job_id)
            if source.attributes["done"]:
                break
        else:
            pytest.fail("no terminal physical action")
        # No physical boundary publication has occurred. The forged active
        # claim must be rejected before publishing its pending final event.
        with store.transaction() as uow:
            job = uow.get_job_state(job_id)
            lease = uow.get_entity("od5a_runner_lease", job_id)
            assert job and lease and lease.state == "released"
            next_trigger = deterministic_id(
                "od5a-recurring-tick", job_id, job.run_count + 1,
            )
            uow.save_job_state(replace(
                job, active_trigger_id=next_trigger,
                status="running", phase="advance",
            ))
            lease.state = "claimed"
            lease.attributes = {
                **lease.attributes, "owner_id": "fabricated-worker",
                "trigger_id": next_trigger,
                "epoch": lease.attributes["epoch"] + 1,
            }
            uow.save_entity(lease)
        before = _truth(store, (job_id,))
        with pytest.raises(ValueError, match="checkpoint contradicts"):
            advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="reconciler",
                now=NOW, lease_duration=LEASE,
                source_trigger_id=next_trigger,
            )
        assert _truth(store, (job_id,)) == before
        assert store.job_state(job_id).active_trigger_id == next_trigger

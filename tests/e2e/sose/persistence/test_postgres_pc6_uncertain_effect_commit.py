"""PC6 #489: actual transport loss at BusinessEffectApplied COMMIT boundary.

The ACK has already committed. An authentic domain StateChart then transitions
to its certified terminal, while a separate PostgreSQL process loses its TCP
transport either before or after the atomic certificate + Command deletion.
Never infer the commit outcome from the worker's exception: query a fresh,
direct PostgreSQL connection, resume the SAME recurring job occurrence, and
compare the entire two-organization causal/physical truth to uninterrupted
execution. This is an infrastructure fault, not a simulated return value.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_infrastructure_chaos import (
    _free_port, _start_proxy,
)
from tests.e2e.sose.persistence.test_postgres_pc6_full_real_domain_recovery import (
    DOMAINS, ORIGIN, INTERVAL, _name, _pending_domain_effect, _proof,
    _run_real_domain_cut, _schedule, _seed_real_workflow, _terminal, _tick,
)
from tests.support.chaos_waits import wait_for_marker_exists, wait_for_process_exit

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PostgreSQL required")

# Fault must execute in an actual subprocess with its own SQL TCP connection.
# The original BusinessEffectService.complete() must retain its real predicates,
# transactional certificate INSERT, and Command DELETE. Only the transport
# around the original UoW boundary is interrupted.
_CERTIFICATE_WORKER = """
import sys, time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from sose.composition.effects import BusinessEffectService
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.postgres import PostgresPersistence

dsn, domain_ns, physical_dsn, physical_ns, correlation, name, phase, slot, marker, release = sys.argv[1:]
slot = int(slot)
old_complete = BusinessEffectService.complete

def wait_for_cut():
    deadline = time.monotonic() + 35
    while not Path(release).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("missing transport fault injection")
        time.sleep(.025)

def faulted_complete(self, *, effect_id, completed_at):
    command = self.persistence.command(effect_id)
    if command is None or command.name != name:
        return old_complete(self, effect_id=effect_id, completed_at=completed_at)
    original_transaction = self._transaction
    @contextmanager
    def cut_transaction():
        with original_transaction() as uow:
            yield uow
            if phase == "before_commit":
                # The real certificate and delete exist only in this still
                # uncommitted SQL transaction. Disconnect must roll both back.
                assert uow.get_business_effect(effect_id) is not None
                assert uow.get_command(effect_id) is None
                Path(marker).write_text(phase)
                wait_for_cut()
        if phase == "after_commit":
            # COMMIT completed; the caller has not yet observed the result.
            # The next SQL operation must fail over the severed real TCP link.
            Path(marker).write_text(phase)
            wait_for_cut()
    self._transaction = cut_transaction
    return old_complete(self, effect_id=effect_id, completed_at=completed_at)

BusinessEffectService.complete = faulted_complete
with PostgresPersistence(dsn, namespace=domain_ns) as domain:
    with PostgresPersistence(physical_dsn, namespace=physical_ns) as physical:
        runner = TradingCustomerRecoveryRunner(
            persistence=domain, owner_id="uncertain-pc6-a",
            job_id="real-pc6-a", max_actions=1,
            resource_persistence=physical,
            resource_policies={"composition.deliver_shipment": ResourcePoolContract(
                resource_type="pickup_courier", pool_id="pc6-real-crossdomain",
                scope="shared", capacity=1,
            )},
            resource_organizations={correlation: "a"},
            resource_slot_duration=timedelta(minutes=5),
            resource_retry_delay=timedelta(minutes=5),
        )
        RecoverySchedule(
            runner, start_at=o2c.ORIGIN, interval=timedelta(hours=1),
            max_slots=1,
        ).run_due(now=o2c.ORIGIN + slot * timedelta(hours=1))
raise AssertionError("certification transaction cut did not interrupt the worker")
"""


def _read_authoritative_outcome(dsn: str, namespace: str, effect_id: str):
    """A new direct SQL session resolves whether the atomic commit happened."""
    with PostgresPersistence(dsn, namespace=namespace) as store:
        with store.boundary_transaction() as uow:
            certificate = uow.get_business_effect(effect_id)
            command = uow.get_command(effect_id)
            matching = [
                delivery for delivery in uow.boundary_deliveries()
                if delivery.consumer_effect_id == effect_id
            ]
            assert len(matching) == 1
            delivery = matching[0]
            consumption = uow.get_boundary_consumption(delivery.delivery_id)
            assert consumption is not None
            assert consumption.consumer_effect_id == effect_id
            assert uow.get_boundary_message(delivery.message_id) is not None
            if certificate is not None:
                assert certificate.effect_id == effect_id
                assert certificate.boundary_message_id == delivery.message_id
                assert command is None
            else:
                assert command is not None
                assert command.command_id == effect_id
            return certificate is not None


def _faulting_world(tmp_path: Path, *, intent_name: str, phase: str):
    assert DSN
    domains = {org: _name("pc6_certificate_" + org) for org in ("a", "b")}
    physical_ns = _name("pc6_certificate_physical")
    seeds = {
        org: _seed_real_workflow(DSN, namespace, org)
        for org, namespace in domains.items()
    }
    slot = 0
    while not _pending_domain_effect(DSN, domains["a"], intent_name):
        assert slot < 18, f"missing durable Command for {intent_name}"
        _tick(DSN, domains["a"], physical_ns, "a", seeds["a"][0], slot)
        slot += 1
    marker = tmp_path / "certificate-cut"
    release = tmp_path / "certificate-release"
    marker.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    proxy = worker = None
    try:
        proxy, proxied_dsn = _start_proxy(
            DSN, port=_free_port(), marker=tmp_path / "proxy-ready",
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", _CERTIFICATE_WORKER,
             proxied_dsn, domains["a"], DSN, physical_ns, seeds["a"][0],
             intent_name, phase, str(slot), str(marker), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_for_marker_exists(
            marker, worker, timeout=50,
            premature_exit_label="certificate worker died before SQL cut",
            timeout_label="certificate worker never entered SQL cut",
            termination_timeout=10,
        )
        assert worker.poll() is None
        proxy.kill()  # terminate real TCP relay while worker connection is live
        wait_for_process_exit(
            proxy, timeout=10, label="killed certificate transport",
            termination_timeout=10,
        )
        release.write_text("fault-injected", encoding="utf-8")
        try:
            stdout, stderr = worker.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            worker.kill()
            stdout, stderr = worker.communicate(timeout=10)
        assert worker.returncode != 0, (
            "client silently accepted an uncertain SQL result",
            stdout[-1200:], stderr[-2500:],
        )
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=10)
        if proxy is not None and proxy.poll() is None:
            proxy.kill()
            proxy.communicate(timeout=10)

    # The wire failure and nonzero worker exit do NOT prove rollback. Only
    # independent durable SQL observation can classify the commit.
    with PostgresPersistence(DSN, namespace=domains["a"]) as store:
        with store.transaction() as uow:
            commands = [c for c in uow.commands() if c.name == intent_name]
        effect_ids = [c.command_id for c in commands]
        certs = [c for c in store.business_effects()
                 if c.entity_type == {
                    DOMAINS[0]: "shipment", DOMAINS[1]: "sales_order",
                 }[intent_name]]
        if phase == "before_commit":
            assert len(effect_ids) == 1 and not certs
            effect_id = effect_ids[0]
        else:
            assert not effect_ids and len(certs) == 1
            effect_id = certs[0].effect_id
        job = store.job_state("real-pc6-a")
        assert job is not None and job.active_trigger_id is not None

    assert _read_authoritative_outcome(DSN, domains["a"], effect_id) is (
        phase == "after_commit"
    )
    # B makes progress with its own clock while A's original job is unclosed.
    _tick(DSN, domains["b"], physical_ns, "b", seeds["b"][0], 0)
    with PostgresPersistence(DSN, namespace=domains["a"]) as domain:
        with PostgresPersistence(DSN, namespace=physical_ns) as shared:
            resumed = _schedule(
                domain, shared, organization="a", correlation=seeds["a"][0],
            ).run_due(now=ORIGIN + slot * INTERVAL)
            assert len(resumed) == 1
            assert domain.job_state("real-pc6-a").next_tick == slot + 1

    for org, start in (("a", slot + 1), ("b", 1)):
        for tick in range(start, 25):
            if _terminal(DSN, domains[org]):
                break
            _tick(DSN, domains[org], physical_ns, org, seeds[org][0], tick)
        assert _terminal(DSN, domains[org]), org
    proof = _proof(DSN, domains, physical_ns, seeds)
    for org in ("a", "b"):
        certificates = proof["organizations"][org]["certificates"]
        assert len(certificates) == 4
        assert len({c["effect_id"] for c in certificates}) == 4
    return proof


@pytest.mark.parametrize("intent_name", DOMAINS[:2], ids=["logistics", "o2c"])
@pytest.mark.parametrize("phase", ("before_commit", "after_commit"))
def test_pg_real_business_certificate_tcp_loss_recovers_full_canonical_truth(
    tmp_path, intent_name, phase,
):
    """Actual TCP loss on the BusinessEffectApplied/Command COMMIT boundary.

    This is a bounded proof of certificate uncertainty, not a simulated
    PostgreSQL server restart nor a complete #489/OD-5a promotion.
    """
    assert DSN
    reference = _run_real_domain_cut(
        DSN, name=intent_name, killed=False,
    )
    recovered = _faulting_world(
        tmp_path, intent_name=intent_name, phase=phase,
    )
    assert recovered == reference, (
        "lost certificate COMMIT response changed cross-domain causality",
        intent_name, phase,
    )

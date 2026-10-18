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
from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict, make_conninfo

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_infrastructure_chaos import (
    REPO_ROOT, _free_port, _start_proxy,
)
from tests.e2e.sose.persistence import test_postgres_pc6_full_real_domain_recovery as full
from tests.e2e.sose.persistence.test_postgres_pc6_full_real_domain_recovery import (
    DOMAINS, ORIGIN, INTERVAL, _canonical_objects, _name,
    _pending_domain_effect, _proof, _run_real_domain_cut,
    _schedule, _seed_real_workflow, _terminal, _tick,
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
from datetime import timedelta
from pathlib import Path
from sose.composition.effects import BusinessEffectService
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.postgres import PostgresPersistence
from psycopg import sql

dsn, domain_ns, physical_dsn, physical_ns, correlation, name, phase, slot, marker, release, arm = sys.argv[1:]
slot = int(slot)
old_complete = BusinessEffectService.complete
old_apply = PostgresPersistence._apply_changes
certificate_target = [None]

def wait_for_cut():
    deadline = time.monotonic() + 35
    while not Path(release).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("missing transport fault injection")
        time.sleep(.025)

def after_real_postgresql_dml(self, before, after, dirty_records):
    # The real store calls _apply_changes only AFTER the in-memory UoW has
    # committed, while its OUTER SQL transaction is still open. Therefore
    # the original INSERT and DELETE have actually reached PostgreSQL before
    # the marker and before the parent breaks this same live TCP connection.
    changed = old_apply(self, before, after, dirty_records)
    effect_id = certificate_target[0]
    if phase == "before_commit" and effect_id and self.namespace == domain_ns:
        rows = self._connection.execute(
            sql.SQL("SELECT collection, record_key FROM {} "
                    "WHERE collection IN ('business_effects', 'commands')").format(
                self._record_table,
            )
        ).fetchall()
        assert any(coll == "business_effects" and effect_id in key
                   for coll, key in rows), (
            "certificate INSERT was not emitted on the live PostgreSQL session"
        )
        assert not any(coll == "commands" and effect_id in key
                       for coll, key in rows), (
            "pending Command DELETE was not emitted on the live PostgreSQL session"
        )
        certificate_target[0] = None
        Path(marker).write_text("after_dml_before_commit")
        wait_for_cut()
    elif phase == "after_commit" and effect_id and self.namespace == domain_ns:
        # Native SQL INSERT/DELETE have finished; the outer COMMIT has NOT
        # been sent. Arm the protocol relay to forward that real COMMIT and
        # drop its *server response* before the client sees success.
        certificate_target[0] = None
        Path(arm).write_text("armed_after_certificate_dml")
    return changed

PostgresPersistence._apply_changes = after_real_postgresql_dml

def faulted_complete(self, *, effect_id, completed_at):
    command = self.persistence.command(effect_id)
    if command is None or command.name != name:
        return old_complete(self, effect_id=effect_id, completed_at=completed_at)
    # Exact certificate identity, not some unrelated SQL write in the
    # same namespace, triggers the pre-COMMIT SQL DML cut above.
    certificate_target[0] = effect_id
    # No post-COMMIT application pause: it was too late to create an
    # uncertain SQL outcome. The proxy instead withholds PostgreSQL's genuine
    # CommandComplete + ReadyForQuery when COMMIT is actually in flight.
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


def _start_commit_reply_drop_proxy(dsn: str, *, port: int, tmp_path: Path):
    """Only the target, armed PostgreSQL COMMIT response may be discarded."""
    ready = tmp_path / "commit-proxy-ready"
    arm = tmp_path / "commit-arm"
    claimed = tmp_path / "commit-claimed"
    dropped = tmp_path / "commit-response-dropped"
    for marker in (ready, arm, claimed, dropped):
        marker.unlink(missing_ok=True)
    details = conninfo_to_dict(dsn)
    host = str(details.get("host") or "").split(",", 1)[0].strip()
    upstream_port = int(str(details.get("port") or "5432").split(",", 1)[0])
    script = REPO_ROOT / "tests" / "support" / "postgres_commit_reply_drop_proxy.py"
    command = [
        sys.executable, str(script), "--listen-port", str(port),
        "--upstream-port", str(upstream_port),
        "--ready", str(ready), "--arm", str(arm),
        "--claimed", str(claimed), "--dropped", str(dropped),
    ]
    if not host or host.startswith("/"):
        command += ["--upstream-unix-socket-dir", host or "/var/run/postgresql"]
    else:
        command += ["--upstream-host", host]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    wait_for_marker_exists(
        ready, process, timeout=30,
        premature_exit_label="COMMIT protocol relay exited before ready",
        timeout_label="COMMIT protocol relay did not become ready",
        termination_timeout=10,
    )
    # The commit-observation proxy must parse plaintext PostgreSQL frames.
    # SSL off only for this local test relay, never the production adapter.
    proxied = make_conninfo(
        dsn, host="127.0.0.1", port=str(port),
        connect_timeout="3", sslmode="disable",
    )
    return process, proxied, arm, claimed, dropped


def _read_authoritative_outcome(dsn: str, namespace: str, effect_id: str):
    """A new direct SQL session resolves whether the atomic commit happened."""
    with PostgresPersistence(dsn, namespace=namespace) as store:
        with store.boundary_transaction(owner_epoch=store.writer_epoch()) as uow:
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


def _full_canonical_truth(dsn, domains, physical_ns, seeds):
    """Include every pending Command beyond the existing immutable #451 oracle."""
    proof = _proof(dsn, domains, physical_ns, seeds)
    for org, namespace in sorted(domains.items()):
        with PostgresPersistence(dsn, namespace=namespace) as store:
            with store.transaction(owner_epoch=store.writer_epoch()) as uow:
                commands = tuple(uow.commands())
            certificates = store.business_effects()
        command_ids = [command.command_id for command in commands]
        certificate_ids = {receipt.effect_id for receipt in certificates}
        assert len(command_ids) == len(set(command_ids)), (
            "duplicate durable Commands under recovered causal boundary"
        )
        assert not (set(command_ids) & certificate_ids), (
            "certified effect is still present as a pending Command"
        )
        proof["organizations"][org]["all_commands"] = _canonical_objects(commands)
    return proof


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
    arm = tmp_path / "commit-arm"
    try:
        if phase == "after_commit":
            proxy, proxied_dsn, arm, claimed, dropped = (
                _start_commit_reply_drop_proxy(
                    DSN, port=_free_port(), tmp_path=tmp_path,
                )
            )
        else:
            proxy, proxied_dsn = _start_proxy(
                DSN, port=_free_port(), marker=tmp_path / "proxy-ready",
            )
        worker = subprocess.Popen(
            [sys.executable, "-c", _CERTIFICATE_WORKER,
             proxied_dsn, domains["a"], proxied_dsn, physical_ns, seeds["a"][0],
             intent_name, phase, str(slot), str(marker), str(release), str(arm)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if phase == "before_commit":
            wait_for_marker_exists(
                marker, worker, timeout=50,
                premature_exit_label="certificate worker died before SQL cut",
                timeout_label="certificate worker never entered SQL cut",
                termination_timeout=10,
            )
            assert worker.poll() is None
            proxy.kill()  # real TCP cut while SQL INSERT/DELETE are uncommitted
            wait_for_process_exit(
                proxy, timeout=10, label="killed certificate transport",
                termination_timeout=10,
            )
            release.write_text("fault-injected", encoding="utf-8")
        else:
            # Do NOT terminate the proxy before the backend sends successful
            # COMMIT. The relay must witness the server's CommandComplete +
            # ReadyForQuery and discard both replies on the target TCP link.
            wait_for_marker_exists(
                dropped, proxy, timeout=50,
                premature_exit_label="COMMIT proxy exited before intercepting response",
                timeout_label="server COMMIT response was never intercepted",
                termination_timeout=10,
            )
            assert dropped.read_text(encoding="utf-8") == (
                "commit_complete_response_dropped"
            ), "server did not certify COMMIT success before the client reply was lost"
            assert claimed.exists() and not arm.exists(), (
                "proxy never intercepted the armed target COMMIT"
            )
        try:
            stdout, stderr = worker.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate(timeout=10)
            pytest.fail("worker never observed the severed TCP transport")
        assert worker.returncode != 0, (
            "client silently accepted an uncertain SQL result",
            stdout[-1200:], stderr[-2500:],
        )
        assert "psycopg" in stderr.lower() and (
            "connection" in stderr.lower() or "operationalerror" in stderr.lower()
        ), (
            "worker exited for a non-transport reason, not a lost SQL response",
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
        # The recovery worker claimed an exclusive writer epoch. SQL reads
        # crossing the active fencing boundary must supply that same epoch;
        # avoid weakening PostgresPersistence's owner validation to audit truth.
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
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
    proof = _full_canonical_truth(DSN, domains, physical_ns, seeds)
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
    # The historical _proof is intentionally unchanged. Extend its evidence
    # for this new certification only, on BOTH reference and recovered arms.
    with patch.object(full, "_proof", side_effect=_full_canonical_truth):
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

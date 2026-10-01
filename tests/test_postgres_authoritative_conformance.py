from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest

from sose.core.events import Command
from sose.core.runtime import DurableStoreItem, ScheduledWork, StoreDefinition, StoreGetRequest, StoreGetResult
from sose.persistence.authoritative_conformance import AuthoritativePersistenceConformanceSuite
from sose.persistence.postgres import PostgresPersistence, StaleWriterError
from sose.persistence.qualification import ConcurrencyEnvelope
from sose.jobs.catalog import build_job_catalog_from_config
from sose.jobs.config import (
    CatalogJobSection,
    DomainSection,
    DomainWarehouseSection,
    PersistenceSection,
    SOSECatalogConfig,
)

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")

def _ns(prefix): return f"{prefix}_{uuid4().hex[:12]}"

class PostgresAuthoritativeHarness:
    concurrency = ConcurrencyEnvelope(max_writers=1, distributed=True)
    def __init__(self): self.namespace = _ns("auth")
    def _open(self, namespace=None):
        assert DSN is not None
        return PostgresPersistence(DSN, namespace=namespace or self.namespace)
    def atomic_uow(self):
        p=self._open(_ns("atomic"))
        try:
            with p.transaction() as u: u.set_committed_tick(1)
            with pytest.raises(RuntimeError):
                with p.transaction() as u:
                    u.set_committed_tick(2); raise RuntimeError("fault")
            assert p.committed_tick()==1
        finally: p.close()
    def read_after_commit(self):
        ns=_ns("read"); a=self._open(ns); b=self._open(ns)
        try:
            with a.transaction() as u: u.set_committed_tick(7)
            assert b.committed_tick()==7
        finally: b.close(); a.close()
    def conditional_ownership(self):
        ns=_ns("claim"); a=self._open(ns); b=self._open(ns)
        try:
            lease=a.claim_writer("a", expected_epoch=0)
            assert lease.epoch==1
            with pytest.raises(StaleWriterError): b.claim_writer("b", expected_epoch=0)
        finally: b.close(); a.close()
    def stale_owner_fencing(self):
        ns=_ns("fence"); a=self._open(ns); b=self._open(ns)
        try:
            la=a.claim_writer("a", expected_epoch=0)
            lb=b.claim_writer("b", expected_epoch=la.epoch)
            with pytest.raises(StaleWriterError):
                with a.transaction(owner_epoch=la.epoch) as u: u.set_committed_tick(9)
            with pytest.raises(StaleWriterError):
                with a.transaction() as u: u.set_committed_tick(10)
            with b.transaction(owner_epoch=lb.epoch) as u: u.set_committed_tick(11)
            assert a.committed_tick()==11
        finally: b.close(); a.close()
    def fresh_process_reconstruction(self):
        ns=_ns("restart"); p=self._open(ns)
        with p.transaction() as u: u.set_committed_tick(13)
        p.close()
        code=("from sose.persistence.postgres import PostgresPersistence;"
              f"p=PostgresPersistence({DSN!r},namespace={ns!r});"
              "assert p.committed_tick()==13;p.close()")
        subprocess.run([sys.executable,"-c",code],check=True)
    def deterministic_continuation(self):
        ref_ns=_ns("ref"); res_ns=_ns("resume")
        def seed(p):
            cmd=Command(command_id="cmd",name="go",entity_type="x",entity_id="1",due_at=NOW)
            work=ScheduledWork(work_id="work",due_at=NOW,priority=1,sequence=1,command_id="cmd")
            with p.transaction() as u:
                u.save_command(cmd); u.save_scheduled_work(work); u.set_committed_tick(20)
        ref=self._open(ref_ns); seed(ref)
        with ref.transaction() as u: u.set_committed_tick(21)
        expected=(ref.committed_tick(),ref.command("cmd"),ref.scheduled_work()); ref.close()
        first=self._open(res_ns); seed(first); first.close()
        resumed=self._open(res_ns)
        with resumed.transaction() as u: u.set_committed_tick(21)
        actual=(resumed.committed_tick(),resumed.command("cmd"),resumed.scheduled_work()); resumed.close()
        assert actual==expected
    def terminal_identity_monotonicity(self):
        p=self._open(_ns("identity")); d=StoreDefinition(name="parts")
        req=StoreGetRequest(request_id="g1",store_name="parts",requested_at=NOW,sequence=1)
        item=DurableStoreItem(item_id="i1",store_name="parts",value={"sku":"A"},priority=0,sequence=1)
        result=StoreGetResult(request_id="g1",store_name="parts",item=item,completed_at=NOW,sequence=2)
        try:
            with p.transaction() as u: u.save_store_definition(d); u.save_store_get_request(req)
            with p.transaction() as u: u.save_store_get_result(result); u.delete_store_get_request("g1")
            with p.transaction() as u: u.save_store_get_result(result)
            with pytest.raises(ValueError, match="already completed"):
                with p.transaction() as u: u.save_store_get_request(req)
        finally: p.close()
    def schema_migration(self):
        assert DSN is not None
        ns=_ns("migration"); meta=f"{ns}_record_meta"; record=f"{ns}_record"
        with psycopg.connect(DSN, autocommit=True) as c:
            c.execute(f'CREATE TABLE "{meta}" (singleton SMALLINT PRIMARY KEY CHECK (singleton=1), schema_version INTEGER NOT NULL, revision BIGINT NOT NULL DEFAULT 0)')
            c.execute(f'CREATE TABLE "{record}" (collection TEXT NOT NULL, record_key TEXT NOT NULL, position BIGINT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(collection,record_key))')
            c.execute(f'INSERT INTO "{meta}" VALUES (1,1,0)')
        p=self._open(ns); p.close()
        with psycopg.connect(DSN, autocommit=True) as c:
            row=c.execute(f'SELECT schema_version,owner_id,owner_epoch FROM "{meta}" WHERE singleton=1').fetchone()
        assert row==(2,None,0)

def test_postgres_earns_authoritative_distributed_single_writer_qualification():
    suite=AuthoritativePersistenceConformanceSuite(PostgresAuthoritativeHarness())
    result=suite.run()
    assert result.failed==()
    assert result.unsupported==frozenset()
    q=suite.qualify(result)
    assert q.authoritative
    assert q.concurrency==ConcurrencyEnvelope(max_writers=1,distributed=True)



def test_postgres_shared_engine_database_isolates_catalog_jobs(tmp_path):
    assert DSN is not None
    suffix = uuid4().hex[:8]
    config = SOSECatalogConfig(
        engine_store=PersistenceSection(
            adapter="postgres",
            options={"dsn": DSN},
        ),
        jobs=[
            CatalogJobSection(
                id=f"orders-{suffix}",
                engine_namespace=f"orders_{suffix}",
                domain=DomainSection(name="order_to_cash"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": f"orders-{suffix}.sqlite3"},
                ),
            ),
            CatalogJobSection(
                id=f"mro-{suffix}",
                engine_namespace=f"mro_{suffix}",
                domain=DomainSection(name="mro"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": f"mro-{suffix}.sqlite3"},
                ),
            ),
        ],
    )
    catalog = build_job_catalog_from_config(config, base_dir=tmp_path)
    try:
        orders = catalog.get(f"orders-{suffix}")
        mro = catalog.get(f"mro-{suffix}")
        assert orders.persistence.dsn == mro.persistence.dsn == DSN
        assert orders.persistence.namespace != mro.persistence.namespace
        assert len(orders.persistence.job_states()) == 1
        assert len(mro.persistence.job_states()) == 1

        orders.run_tick(trigger_id="orders:1")
        assert orders.state().next_tick == 1
        assert mro.state().next_tick == 0

        mro.run_tick(trigger_id="mro:1")
        assert mro.state().next_tick == 1
        assert orders.state().next_tick == 1
    finally:
        catalog.close()

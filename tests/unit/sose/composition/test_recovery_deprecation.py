"""#634: policy-less trading-company recovery is deprecated (removed in 0.9)."""
from __future__ import annotations

import json
import warnings
from argparse import Namespace

import pytest

from sose.cli_validation import _recovery_resource_policies, _recovery_store
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _runner(store, **extra):
    return TradingCustomerRecoveryRunner(
        persistence=store, owner_id="worker",
        organization_owners={"flow": "org"}, **extra,
    )


def test_policy_less_runner_warns_with_removal_version_at_the_call_site(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _runner(store)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    message = str(deprecations[0].message)
    assert "without resource_policies" in message
    assert "0.8.1" in message and "0.9.0" in message
    assert "resource_policies" in message.split(";", 1)[1]
    assert deprecations[0].filename == __file__   # attributed to the constructing caller


def test_cli_policy_file_loads_pool_contracts(tmp_path):
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({
        "composition.deliver_shipment": {
            "resource_type": "pickup_courier", "pool_id": "couriers",
            "scope": "shared", "capacity": 2,
        },
    }), encoding="utf-8")
    args = Namespace(resource_policies=str(path), postgres_dsn="postgresql://x")
    assert _recovery_resource_policies(args) == {
        "composition.deliver_shipment": ResourcePoolContract(
            resource_type="pickup_courier", pool_id="couriers", scope="shared", capacity=2,
        ),
    }


@pytest.mark.parametrize("content", ["{}", "[]"])
def test_cli_policy_file_must_be_a_non_empty_object(tmp_path, content):
    path = tmp_path / "policies.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty JSON object"):
        _recovery_resource_policies(Namespace(resource_policies=str(path), postgres_dsn="x"))


def test_cli_policies_require_postgres(tmp_path):
    with pytest.raises(ValueError, match="requires --postgres-dsn"):
        _recovery_resource_policies(Namespace(resource_policies="p.json", postgres_dsn=None))
    assert _recovery_resource_policies(Namespace(resource_policies=None, postgres_dsn=None)) is None


def test_cli_store_selection_defaults_to_sqlite(tmp_path):
    store = _recovery_store(Namespace(postgres_dsn=None, sqlite=str(tmp_path / "s.sqlite")))
    try:
        assert isinstance(store, SQLiteIncrementalPersistence)
    finally:
        store.close()

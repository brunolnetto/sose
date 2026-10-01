from __future__ import annotations

import pytest

from sose.examples.catalog import builtin_catalog
from tests.support.behavioral_conformance import assert_restart_equivalent


CANONICALS = builtin_catalog().names(kind="canonical")



@pytest.mark.parametrize("name", CANONICALS)
def test_continuous_and_restarted_execution_are_equivalent(tmp_path, name):
    assert_restart_equivalent(
        definition=builtin_catalog().get(name),
        directory=tmp_path,
        ticks=4,
        restart_at=(2,),
    )


def test_catalog_classifies_canonicals_without_changing_default_listing():
    catalog = builtin_catalog()
    assert set(CANONICALS) == {
        "producer_consumer", "sleeping_barber", "dining_philosophers",
        "readers_writers", "job_shop",
    }
    assert len(catalog.names(kind="domain")) == 22
    assert len(catalog.names()) == 27
    assert set(catalog.names(kind="canonical")).isdisjoint(
        catalog.names(kind="domain")
    )

@pytest.mark.parametrize("restart_at", [(1,), (3,), (1, 3)])
@pytest.mark.parametrize("name", CANONICALS)
def test_canonical_is_equivalent_across_adversarial_restart_points(
    tmp_path, name, restart_at
):
    assert_restart_equivalent(
        definition=builtin_catalog().get(name),
        directory=tmp_path,
        ticks=6,
        restart_at=restart_at,
        job_id=f"{name}-adversarial-{'-'.join(map(str, restart_at))}",
    )

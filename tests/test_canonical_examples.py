from __future__ import annotations

import pytest

from sose.examples.catalog import builtin_catalog
from sose.jobs.scaffold import render_sose_toml


CANONICALS = (
    "producer_consumer",
    "sleeping_barber",
    "dining_philosophers",
    "readers_writers",
    "job_shop",
)


@pytest.mark.parametrize("name", CANONICALS)
def test_canonical_is_wired_as_regular_domain(name):
    definition = builtin_catalog().get(name)
    config = definition.default_config()
    assert config.start_at is not None
    rendered = render_sose_toml(definition)
    assert f'name = "{name}"' in rendered
    assert "[domain.parameters]" in rendered
    assert "[engine_store]" in rendered
    assert "[runtime]" not in rendered


def test_catalog_contains_business_and_canonical_examples():
    names = set(builtin_catalog().names())
    assert set(CANONICALS) <= names
    assert {"mro", "manufacturing", "hospitals"} <= names

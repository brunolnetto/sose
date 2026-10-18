"""#528 PostgreSQL: persist typed nested Entity deltas, not Python == aliases."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from sose.domain.entity import Entity
from sose.persistence.postgres import PostgresPersistence

DSN = os.getenv("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="live PostgreSQL required")


@pytest.mark.parametrize(
    ("initial", "changed"),
    [(0, False), (0.0, False), (1, True), (1.0, True),
     (0, 0.0), (1, 1.0), (False, 0)],
)
def test_pg_nested_type_only_change_is_durable_after_independent_reopen(
    initial, changed,
):
    assert DSN
    ns = "typed_dirty_" + uuid4().hex[:12]
    target_key = ("typed-dirty", "owner")
    untouched_key = ("typed-dirty", "peer")
    initial_target = Entity(
        id=target_key[1], entity_type=target_key[0],
        state="active",
        attributes={"nested": {"action": {"at": initial, "seq": initial}}},
    )
    unaffected = Entity(
        id=untouched_key[1], entity_type=untouched_key[0],
        state="ready", attributes={"causal": "independent"},
    )
    with PostgresPersistence(DSN, namespace=ns) as connection:
        with connection.transaction() as uow:
            uow.save_entity(initial_target)
            uow.save_entity(unaffected)
        assert initial == changed
        with connection.transaction() as uow:
            mutable = uow.get_entity(*target_key)
            assert mutable is not None
            mutable.attributes["nested"]["action"]["at"] = changed
            mutable.attributes["nested"]["action"]["seq"] = changed
            uow.save_entity(mutable)
        # No staging marker or second write: the real single-UoW dirty
        # tracker must persist the type-only change by itself.
    with PostgresPersistence(DSN, namespace=ns) as independent:
        target = independent.entity(*target_key)
        peer = independent.entity(*untouched_key)
        assert target is not None
        assert peer == unaffected
        for key in ("at", "seq"):
            value = target.attributes["nested"]["action"][key]
            assert value == changed
            assert type(value) is type(changed)
        with independent.transaction() as uow:
            uow.save_entity(target)
        # Reordering a nested mapping without changing a typed value must
        # not rewrite the record or bump the shared PostgreSQL revision.
        before_revision = independent._database_revision()
        reordered = independent.entity(*target_key)
        assert reordered is not None
        reordered.attributes["nested"]["action"] = {
            "seq": changed, "at": changed,
        }
        with independent.transaction() as uow:
            uow.save_entity(reordered)
        assert independent._database_revision() == before_revision
    with PostgresPersistence(DSN, namespace=ns) as again:
        assert again.entity(*target_key) == target
        assert again.entity(*untouched_key) == unaffected

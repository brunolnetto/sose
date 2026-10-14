"""Reject ambiguous pre-owner temporal authority during record reconstruction."""
import pytest

from sose.persistence.codec import dumps
from sose.persistence.records import StateRecord, records_to_state


@pytest.mark.parametrize("collection", [
    "simulation_positions", "scenario_states", "committed_ticks",
])
@pytest.mark.parametrize("owner_key", [None, "", "  ", 123])
def test_unowned_or_invalid_temporal_key_fails_closed(collection, owner_key):
    record = StateRecord(
        collection=collection,
        key=dumps(owner_key),
        position=0,
        payload=dumps(1),
    )
    with pytest.raises(ValueError, match="unowned durable temporal record"):
        records_to_state([record])


def test_valid_qualified_temporal_checkpoint_key_is_preserved():
    state = records_to_state([
        StateRecord(
            collection="committed_ticks",
            key=dumps("organization-one"),
            position=0,
            payload=dumps(12),
        ),
    ])
    assert state.committed_ticks == {"organization-one": 12}


@pytest.mark.parametrize("collection", [
    "simulation_position", "scenario_state", "committed_tick",
])
def test_removed_scalar_collection_is_not_silently_discarded(collection):
    with pytest.raises(ValueError, match="unsupported legacy unowned runtime records"):
        records_to_state([StateRecord(
            collection=collection, key="__scalar__", position=0, payload=dumps(0),
        )])

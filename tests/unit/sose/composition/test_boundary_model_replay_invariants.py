"""TDD malformed boundary history must fail closed before restart replay."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.model import (
    BoundaryConsumption, BoundaryDelivery, BoundaryLease, BoundaryMessage,
    DeliveryStatus,
)


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def source():
    return BoundaryMessage.create(
        contract_name="accounting.entry_requested", contract_version=1,
        source_domain="cards_payments", source_identity="payment-1",
        destination_domain="record_to_report", occurrence_key="journal-1",
        correlation_id="order-1", causation_id=None, produced_at=NOW,
        payload={"amount": 1, "currency": "USD"},
    )


@pytest.mark.parametrize("payload", [None, [1, 2], "text", 42])
def test_boundary_payload_must_be_mapping(payload):
    with pytest.raises(TypeError, match="must be a mapping"):
        BoundaryMessage.create(
            contract_name="test", contract_version=1, source_domain="from",
            source_identity="id", destination_domain="to",
            occurrence_key="key", correlation_id="corr",
            causation_id=None, produced_at=NOW, payload=payload,
        )


@pytest.mark.parametrize("payload", [
    {"amount": float("nan")}, {"bad": object()}, {"bad": {1, 2}},
])
def test_boundary_payload_is_strict_json_not_implicit_repr(payload):
    with pytest.raises(TypeError, match="canonical JSON"):
        BoundaryMessage.create(
            contract_name="test", contract_version=1, source_domain="from",
            source_identity="id", destination_domain="to",
            occurrence_key="key", correlation_id="corr",
            causation_id=None, produced_at=NOW, payload=payload,
        )


@pytest.mark.parametrize("field", [
    "message_id", "contract_name", "source_domain", "source_identity",
    "destination_domain", "occurrence_key", "correlation_id",
    "payload_json", "payload_hash",
])
def test_message_mandatory_fields_must_be_nonempty(field):
    with pytest.raises(ValueError, match="cannot be empty"):
        replace(source(), **{field: ""})


@pytest.mark.parametrize("mutation,reason", [
    ({"contract_version": 0}, "contract_version must be positive"),
    ({"causation_kind": "other"}, "unsupported causal reference kind"),
    ({"causation_kind": "boundary"}, "typed causation requires"),
    ({"causation_kind": "event"}, "typed causation requires"),
    ({"payload_json": "not_json"}, "valid JSON"),
    ({"payload_json": '{"amount":1, "currency":"USD"}'}, "canonical encoding"),
    ({"payload_hash": "wrong"}, "payload_hash does not match"),
    ({"payload_json": "[1,2]"}, "payload must be a mapping"),
])
def test_message_rejects_invalid_durable_encoding(mutation, reason):
    with pytest.raises((TypeError, ValueError), match=reason):
        replace(source(), **mutation)


def test_message_payload_rechecks_decoded_object_shape():
    # Represent bytes from an external corrupted DB, bypassing the constructor
    # which rightly rejects non-mapping payload_json.
    corrupt = object.__new__(BoundaryMessage)
    object.__setattr__(corrupt, "payload_json", "[1,2]")
    with pytest.raises(TypeError, match="decode to an object"):
        corrupt.payload()


def test_valid_typed_boundary_and_contract_key():
    message = replace(source(), causation_id="cause-1", causation_kind="boundary")
    assert message.contract_key == "accounting.entry_requested.v1"
    assert message.payload()["amount"] == 1
    assert message.causation_id == "cause-1"


@pytest.mark.parametrize("mutation,reason", [
    ({"delivery_id": ""}, "identity cannot be empty"),
    ({"message_id": ""}, "identity cannot be empty"),
    ({"destination_domain": ""}, "contract cannot be empty"),
    ({"contract_name": ""}, "contract cannot be empty"),
    ({"contract_version": 0}, "contract_version must be positive"),
    ({"attempts": -1}, "counters must be non-negative"),
    ({"claim_epoch": -1}, "counters must be non-negative"),
    ({"claimed_at": NOW}, "pending delivery cannot carry"),
    ({"claim_owner_id": "worker"}, "pending delivery cannot carry"),
    ({"consumer_effect_id": "applied"}, "pending delivery cannot carry"),
    ({"status": DeliveryStatus.CLAIMED}, "requires owner and lease"),
    ({"status": DeliveryStatus.CONSUMED}, "requires effect and timestamp"),
])
def test_delivery_record_rejects_corruption(mutation, reason):
    with pytest.raises(ValueError, match=reason):
        replace(BoundaryDelivery.pending(source()), **mutation)


def test_claimed_delivery_requires_positive_fencing_epoch():
    delivery = BoundaryDelivery.pending(source())
    with pytest.raises(ValueError, match="positive fencing epoch"):
        replace(
            delivery, status=DeliveryStatus.CLAIMED,
            claim_owner_id="worker", claimed_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )


def test_claimed_delivery_cannot_be_marked_consumed():
    delivery = BoundaryDelivery.pending(source())
    with pytest.raises(ValueError, match="claimed delivery cannot be consumed"):
        replace(
            delivery, status=DeliveryStatus.CLAIMED, claim_owner_id="worker",
            claim_epoch=1, claimed_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            consumed_at=NOW, consumer_effect_id="applied",
        )


@pytest.mark.parametrize("mutation", [
    {"delivery_id": ""}, {"message_id": ""}, {"owner_id": ""}, {"epoch": 0},
])
def test_boundary_lease_must_have_current_identity_and_epoch(mutation):
    lease = BoundaryLease(
        delivery_id="delivery", message_id="message", owner_id="worker",
        epoch=1, lease_expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ValueError):
        replace(lease, **mutation)


@pytest.mark.parametrize("mutation", [
    {"consumption_id": ""}, {"delivery_id": ""}, {"message_id": ""},
    {"consumer_effect_id": ""},
])
def test_boundary_consumption_requires_immutable_receipt_identity(mutation):
    delivery = BoundaryDelivery.pending(source())
    receipt = BoundaryConsumption.create(
        delivery=delivery, consumer_effect_id="effect", consumed_at=NOW,
    )
    with pytest.raises(ValueError):
        replace(receipt, **mutation)

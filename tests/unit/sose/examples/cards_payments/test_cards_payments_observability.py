from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from sose.examples.cards_payments.config import CardsPaymentsConfig
from sose.examples.cards_payments.definition import definition
from sose.examples.cards_payments.observability import (
    cards_payments_kpis,
    cards_payments_projection,
)
from sose.examples.cards_payments.process_audit import process_manifest
from sose.examples.cards_payments.simulation import (
    run_dispute_path,
    run_happy_path,
    run_refund_path,
    run_retry_path,
    seed_reference,
)
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.persistence.memory import MemoryPersistence


def test_cards_projection_is_read_only_and_idempotent() -> None:
    persistence, entities = run_happy_path()
    payment = persistence.entity("card_payment", entities.payment_id)
    assert payment is not None
    before_version = payment.version

    left = cards_payments_projection(persistence, entities=entities)
    right = cards_payments_projection(persistence, entities=entities)

    assert left == right
    assert left.payment_id == entities.payment_id
    assert left.payment_state == "settled"
    assert left.amount == 125.0
    assert left.currency == "USD"
    assert left.settled is True
    assert left.refunded is False
    assert left.terminal_outcome is None
    assert left.settlement_lead_time_seconds is not None
    assert left.settlement_lead_time_seconds >= 0.0

    after = persistence.entity("card_payment", entities.payment_id)
    assert after is not None and after.version == before_version


def test_cards_projection_does_not_invent_settlement_before_execution() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)

    projection = cards_payments_projection(persistence, entities=entities)
    kpis = cards_payments_kpis(persistence, entities=entities)

    assert projection.payment_state == "authorization_requested"
    assert projection.settled is False
    assert projection.refunded is False
    assert projection.terminal_outcome is None
    assert projection.settlement_lead_time_seconds is None
    assert kpis.transition_count == 0
    assert kpis.settlement_lead_time_seconds is None
    assert kpis.settled is False


def test_cards_settlement_timestamp_survives_later_refund() -> None:
    settled_persistence, settled_entities = run_happy_path()
    refund_persistence, refund_entities = run_refund_path()

    settled = cards_payments_projection(
        settled_persistence,
        entities=settled_entities,
    )
    refunded = cards_payments_projection(
        refund_persistence,
        entities=refund_entities,
    )
    refund_kpis = cards_payments_kpis(
        refund_persistence,
        entities=refund_entities,
    )

    assert settled.settlement_lead_time_seconds is not None
    assert refunded.payment_state == "refunded"
    assert refunded.refunded is True
    assert refunded.terminal_outcome == "refunded"
    assert refunded.settled is True
    assert (
        refunded.settlement_lead_time_seconds
        == settled.settlement_lead_time_seconds
    )
    assert refund_kpis.refund_count == 1


def test_cards_kpis_preserve_retry_and_dispute_history() -> None:
    retry_persistence, retry_entities = run_retry_path()
    retry_kpis = cards_payments_kpis(retry_persistence, entities=retry_entities)

    assert retry_kpis.settled is True
    assert retry_kpis.settlement_retry_count == 1
    assert retry_kpis.authorization_decline_count == 0

    dispute_persistence, dispute_entities = run_dispute_path(outcome="merchant")
    projection = cards_payments_projection(
        dispute_persistence,
        entities=dispute_entities,
    )
    dispute_kpis = cards_payments_kpis(
        dispute_persistence,
        entities=dispute_entities,
    )

    assert projection.payment_state == "settled"
    assert projection.dispute_id is not None
    assert projection.dispute_state == "merchant_won"
    assert projection.dispute_open is False
    assert dispute_kpis.dispute_count == 1
    assert dispute_kpis.chargeback_count == 1
    assert dispute_kpis.settled is True
    assert set(asdict(dispute_kpis)) == {
        "settlement_lead_time_seconds",
        "amount",
        "transition_count",
        "authorization_decline_count",
        "settlement_retry_count",
        "refund_count",
        "dispute_count",
        "chargeback_count",
        "settled",
    }


def test_cards_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.kpis == frozenset(
        {
            "settlement_lead_time_seconds",
            "amount",
            "transition_count",
            "authorization_decline_count",
            "settlement_retry_count",
            "refund_count",
            "dispute_count",
            "chargeback_count",
            "settled",
        }
    )
    for evidence in (
        ProcessEvidence.KPIS,
        ProcessEvidence.ERD,
        ProcessEvidence.STATECHART_DOCUMENTATION,
        ProcessEvidence.PROCESS_DIAGRAM,
        ProcessEvidence.PROJECTION_CONTRACT,
        ProcessEvidence.CONFIGURATION_DOCUMENTATION,
    ):
        assert evidence in manifest.evidence
        assert manifest.evidence_sources[evidence]


def test_cards_normative_pc5_documentation_covers_process_and_config() -> None:
    specification = Path("docs/examples/cards-payments/specification.md").read_text(
        encoding="utf-8"
    )

    assert "flowchart TD" in specification

    for field_name in CardsPaymentsConfig.model_fields:
        assert f"`{field_name}`" in specification

    for field_name in definition.runtime_mutable_fields:
        assert f"`{field_name}`" in specification

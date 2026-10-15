from sose.examples.record_to_report.simulation import (
    adjustment_id,
    run_adjustment_path,
    run_happy_path,
)


REFERENCE_OWNER_ID = "record-to-report-reference-organization"

def test_r2r_happy_path_closes_period_and_releases_capacity():
    persistence, entities = run_happy_path(owner_id=REFERENCE_OWNER_ID)

    period = persistence.entity("accounting_period", entities.period_id)
    journal = persistence.entity("journal_entry", entities.journal_id)
    reconciliation = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    task = persistence.entity("close_task", entities.close_task_id)

    assert period is not None and period.state == "closed"
    assert journal is not None and journal.state == "posted"
    assert reconciliation is not None and reconciliation.state == "matched"
    assert task is not None and task.state == "completed"
    assert persistence.scheduled_work() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()


def test_unmatched_item_uses_immutable_adjustment_path_before_close():
    persistence, entities = run_adjustment_path(owner_id=REFERENCE_OWNER_ID)

    reconciliation = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    adjustment = persistence.entity(
        "accounting_adjustment",
        adjustment_id(entities.reconciliation_id),
    )
    period = persistence.entity("accounting_period", entities.period_id)

    assert reconciliation is not None and reconciliation.state == "reconciled"
    assert adjustment is not None and adjustment.state == "posted"
    assert period is not None and period.state == "closed"

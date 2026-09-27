from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 10, 1, tzinfo=timezone.utc)


def catastrophe_capacity_scenario() -> Scenario:
    return Scenario(
        name="insurance-catastrophe-capacity-strain",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=5),
        effects=(AttributeEffect("insurance.adjuster.available", False),),
    )


def payment_processor_outage_scenario() -> Scenario:
    return Scenario(
        name="insurance-payment-processor-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("insurance.payment.available", False),),
    )

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2027, 1, 5, 8, tzinfo=timezone.utc)


def macroeconomic_stress_scenario() -> Scenario:
    return Scenario(
        name="credit-loans-macroeconomic-stress",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(
            AttributeEffect("credit_loans.underwriting.available", False),
            AttributeEffect("credit_loans.collection.available", False),
            AttributeEffect("credit_loans.stress.active", True),
        ),
    )

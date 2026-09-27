from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 6, 1, tzinfo=timezone.utc)


def occupancy_surge_scenario() -> Scenario:
    return Scenario(
        name="hospital-occupancy-surge",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=8),
        effects=(AttributeEffect("hospital.ward.available", False),),
    )


def emergency_procedure_surge_scenario() -> Scenario:
    return Scenario(
        name="hospital-emergency-procedure-surge",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=2),
        effects=(AttributeEffect("hospital.procedure.emergency", True),),
    )

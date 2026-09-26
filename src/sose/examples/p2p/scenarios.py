from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


SCENARIO_ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def supplier_delay_scenario() -> Scenario:
    return Scenario(
        name="p2p-supplier-delay",
        trigger=ScheduledTrigger(at=SCENARIO_ORIGIN),
        duration=timedelta(hours=6),
        effects=(
            AttributeEffect("p2p.supplier.delay", True),
            AttributeEffect("p2p.supplier.lead_time_multiplier", 2.0),
        ),
    )


def demand_spike_scenario() -> Scenario:
    return Scenario(
        name="p2p-demand-spike",
        trigger=ScheduledTrigger(at=SCENARIO_ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("p2p.demand.multiplier", 2.0),),
    )


def receiving_congestion_scenario() -> Scenario:
    return Scenario(
        name="p2p-receiving-congestion",
        trigger=ScheduledTrigger(at=SCENARIO_ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("p2p.receiving.capacity_factor", 0.5),),
    )

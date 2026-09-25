from datetime import datetime, timedelta, timezone

from sose.core.events import Command
from sose.core.scheduler import Scheduler


def cmd(name: str, at: datetime) -> Command:
    return Command(name, name, "x", name, at)


def test_scheduler_orders_by_time_then_priority():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scheduler = Scheduler()
    scheduler.schedule(cmd("later", t0 + timedelta(hours=1)))
    scheduler.schedule(cmd("low-priority", t0), priority=20)
    scheduler.schedule(cmd("high-priority", t0), priority=10)

    assert [c.name for c in scheduler.due(t0)] == ["high-priority", "low-priority"]
    assert [c.name for c in scheduler.due(t0 + timedelta(hours=1))] == ["later"]

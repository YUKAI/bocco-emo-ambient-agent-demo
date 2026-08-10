from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from bocco_bridge.db import EventDatabase
from bocco_bridge.scheduler import ProactiveScheduler


class ProactiveSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.database = EventDatabase(self.path)
        await self.database.initialize()
        self.notifications = 0

        def notify() -> None:
            self.notifications += 1

        self.notify = notify

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_due_time_fires_once_per_local_date(self) -> None:
        await self.database.add_schedule(
            "source-daily",
            "room-1",
            "08:30",
            "custom",
            "朝の声かけ",
        )
        local_now = datetime(
            2026, 8, 3, 8, 30, 20, tzinfo=timezone(timedelta(hours=9))
        )
        scheduler = ProactiveScheduler(
            self.database, self.notify, now=lambda: local_now
        )

        self.assertEqual(await scheduler.check_once(), 1)
        self.assertEqual(await scheduler.check_once(), 0)
        first = await self.database.get_event("internal:schedule:1:2026-08-03")
        assert first is not None
        self.assertEqual(first.event_type, "schedule.custom")
        self.assertEqual(first.speech_text, "朝の声かけ")

        local_now += timedelta(days=1)
        self.assertEqual(await scheduler.check_once(), 1)
        self.assertEqual(self.notifications, 2)
        schedules = await self.database.list_schedules("room-1")
        self.assertEqual(schedules[0].last_fired_date, "2026-08-04")

    async def test_restart_during_fired_minute_does_not_refire(self) -> None:
        await self.database.add_schedule(
            "source-restart", "room-1", "09:00", "briefing", ""
        )
        local_now = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
        first = ProactiveScheduler(self.database, self.notify, now=lambda: local_now)
        self.assertEqual(await first.check_once(), 1)

        reopened = EventDatabase(self.path)
        await reopened.initialize()
        restarted = ProactiveScheduler(reopened, self.notify, now=lambda: local_now)

        self.assertEqual(await restarted.check_once(), 0)
        self.assertEqual(await reopened.queue_counts(), {"pending": 1})

    async def test_missed_minute_is_not_retroactively_fired(self) -> None:
        await self.database.add_schedule(
            "source-missed", "room-1", "07:15", "custom", "起きる時間"
        )
        after_due = datetime(2026, 8, 3, 7, 16, tzinfo=timezone.utc)
        scheduler = ProactiveScheduler(
            self.database, self.notify, now=lambda: after_due
        )

        self.assertEqual(await scheduler.check_once(), 0)
        self.assertEqual(await self.database.queue_counts(), {})
        schedules = await self.database.list_schedules("room-1")
        self.assertIsNone(schedules[0].last_fired_date)

    async def test_due_time_uses_supplied_pi_local_timezone_and_weekday(self) -> None:
        monday_only = 1 << 0
        await self.database.add_schedule(
            "source-timezone",
            "room-1",
            "00:15",
            "custom",
            "現地時間の予定",
            weekday_mask=monday_only,
        )
        local_monday = datetime(
            2026, 8, 3, 0, 15, tzinfo=timezone(timedelta(hours=9))
        )
        self.assertEqual(local_monday.astimezone(timezone.utc).weekday(), 6)
        scheduler = ProactiveScheduler(
            self.database, self.notify, now=lambda: local_monday
        )

        self.assertEqual(await scheduler.check_once(), 1)

    async def test_naive_scheduler_time_is_rejected(self) -> None:
        scheduler = ProactiveScheduler(
            self.database, self.notify, now=lambda: datetime(2026, 8, 3, 8, 0)
        )

        with self.assertRaisesRegex(ValueError, "Pi-local timezone"):
            await scheduler.check_once()


if __name__ == "__main__":
    unittest.main()

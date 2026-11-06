"""floor_scheduler.py: new users every tick, every save once a day, caught up after a restart."""
import io
import os
import contextlib
import datetime as dt
import unittest
from unittest import mock

import floor_scheduler as fs
import floor_new_users as fn
from tests.support import RpcnDataCase, char_state, make_save, read

def at(day, hour, minute=0):
    """a time in KST, the zone the daily floor is scheduled in"""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=fn.KST)


class DailyDueTest(unittest.TestCase):
    def test_is_due_at_0610_kst_whatever_zone_now_is_in(self):
        utc = dt.timezone.utc
        self.assertTrue(fs.daily_due(dt.datetime(2026, 11, 5, 21, 10, tzinfo=utc), at(5, 6, 10)))
        self.assertFalse(fs.daily_due(dt.datetime(2026, 11, 6, 6, 10, tzinfo=utc), at(6, 6, 10)))

    def test_due_when_it_never_ran(self):
        self.assertTrue(fs.daily_due(at(6, 3), None))

    def test_due_once_todays_time_has_passed(self):
        self.assertTrue(fs.daily_due(at(6, 6, 10), at(5, 6, 10)))

    def test_not_due_before_todays_time_after_yesterdays_run(self):
        self.assertFalse(fs.daily_due(at(6, 6, 9), at(5, 6, 10)))

    def test_not_due_again_on_the_same_day(self):
        self.assertFalse(fs.daily_due(at(6, 23), at(6, 6, 10)))

    def test_catches_up_a_day_missed_while_down(self):
        self.assertTrue(fs.daily_due(at(6, 3), at(4, 6, 10)))


class TickTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.last_daily = os.path.join(self.backup_dir, "floor_daily.json")
        for p in (mock.patch.object(fs, "LAST_DAILY", self.last_daily),
                  mock.patch.multiple(fn, STATE=os.path.join(self.root, "floor_new_users.json"),
                                      TEMPLATE=os.path.join(self.root, "template_new_user.tdt"))):
            p.start()
            self.addCleanup(p.stop)
        fn.save_state({"cutoff": fn.CUTOFF.isoformat(), "done": {}})
        self.unfloored = bytes(make_save(chars=[(0, 30, 100, 5, 5)], account_rank=30))

    def tick(self, now):
        with contextlib.redirect_stdout(io.StringIO()):
            fs.tick(now)

    def failing_tick(self, now):
        """tick(), returning the failures it logged"""
        with self.assertLogs(fs.log, "ERROR") as logs:
            self.tick(now)
        return "\n".join(logs.output)

    def batches(self):
        return [r for r in self.audit_records() if r["action"] == "floor-batch"]

    def test_floors_every_save_once_a_day(self):
        old = self.add_account("Old", self.unfloored)
        other = self.add_account("Other", self.unfloored)
        self.tick(at(6, 6, 10))
        self.tick(at(6, 6, 11))
        self.assertEqual((char_state(read(old), 1)[0], char_state(read(other), 1)[0]), (19, 19))
        self.assertEqual(len(self.batches()), 1)
        self.assertEqual(fs.last_daily(), at(6, 6, 10))

    def test_floors_a_new_user_on_every_tick(self):
        new = self.add_account("New", self.unfloored, created=int(fn.CUTOFF.timestamp()) + 100)
        fs.record_daily(at(6, 6, 10))
        self.tick(at(6, 7))
        self.assertEqual(char_state(read(new), 1)[0], 19)
        self.assertEqual(fn.load_state()["done"].keys(), {"New"})
        self.assertEqual(self.batches(), [])

    def test_retries_a_daily_floor_that_could_not_check_who_is_online(self):
        path = self.add_account("Old", self.unfloored)
        self.online = None
        out = self.failing_tick(at(6, 6, 10))
        self.assertIn("daily floor failed", out)
        self.assertEqual((read(path), fs.last_daily()), (self.unfloored, None))

        self.online = set()
        self.tick(at(6, 6, 11))
        self.assertEqual(char_state(read(path), 1)[0], 19)

    def test_a_new_users_failure_does_not_stop_the_daily_floor(self):
        path = self.add_account("Old", self.unfloored)
        os.unlink(fn.STATE)
        out = self.failing_tick(at(6, 6, 10))
        self.assertIn("new users floor failed", out)
        self.assertEqual(char_state(read(path), 1)[0], 19)


class StartTest(unittest.TestCase):
    def test_off_starts_nothing(self):
        with mock.patch.dict(os.environ, {"FLOOR_SCHEDULER": "off"}), self.assertLogs(fs.log):
            self.assertIsNone(fs.start())


if __name__ == "__main__":
    unittest.main()

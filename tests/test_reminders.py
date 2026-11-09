"""Tests for reminders and scheduling (nova_reminders + nova_gui glue).

Run from the project folder:  python -m unittest discover -s tests -v

Every test uses fixed timestamps (no dependence on today's date) and a
temporary database. Daylight-saving behaviour is tested with a small
EU-style time zone defined below, since Nova's own PC zone (IST) has no DST.
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_reminders as nr  # noqa: E402

SAT = datetime(2026, 11, 7, 13, 0)   # Saturday 7 November 2026, 1:00 PM


class EuropeTZ(tzinfo):
    """UTC+1, with DST (UTC+2) from the last Sunday of March to the last Sunday of October, 01:00 UTC."""
    STD, DST = timedelta(hours=1), timedelta(hours=1)

    @staticmethod
    def _last_sunday(year, month):
        day = datetime(year, month + 1, 1) - timedelta(days=1)
        return day - timedelta(days=(day.weekday() + 1) % 7)

    def _bounds(self, year):  # UTC instants
        return (self._last_sunday(year, 3).replace(hour=1), self._last_sunday(year, 10).replace(hour=1))

    def utcoffset(self, dt):
        start, end = self._bounds(dt.year)
        wall = dt.replace(tzinfo=None)
        start_wall, end_wall = start + self.STD, end + self.STD + self.DST   # 02:00 and 03:00 local
        if start_wall <= wall < start_wall + self.DST:      # the skipped hour
            return self.STD + (self.DST if dt.fold else timedelta(0))
        if end_wall - self.DST <= wall < end_wall:          # the repeated hour
            return self.STD + (timedelta(0) if dt.fold else self.DST)
        return self.STD + self.DST if start_wall + self.DST <= wall < end_wall - self.DST else self.STD

    def dst(self, dt):
        return self.utcoffset(dt) - self.STD

    def tzname(self, dt):
        return "TEST"

    def fromutc(self, dt):
        utc = dt.replace(tzinfo=None)
        start, end = self._bounds(utc.year)
        if start <= utc < end:
            return (utc + self.STD + self.DST).replace(tzinfo=self)
        if end <= utc < end + self.DST:
            return (utc + self.STD).replace(tzinfo=self, fold=1)
        return (utc + self.STD).replace(tzinfo=self)


TZ = EuropeTZ()


def parse(text, now=SAT, tz=None):
    return nr.parse_reminder(text, now, tz)


class ParserTests(unittest.TestCase):
    def ok(self, text, due, title, rule=None, anchor=None, now=SAT):
        result = parse(text, now)
        self.assertEqual(result.kind, "ok", result.message)
        self.assertEqual((result.title, result.schedule.due, result.schedule.rule, result.schedule.anchor),
                         (title, due, rule, anchor))
        return result

    def ask(self, text, fragment, now=SAT):
        result = parse(text, now)
        self.assertEqual(result.kind, "clarify", result.message)
        self.assertIn(fragment, result.message)
        return result

    def test_examples_from_the_brief(self):
        self.ok("Remind me tomorrow at 9 AM to study.", datetime(2026, 11, 8, 9, 0), "study")
        self.ok("Remind me every Monday at 8 PM to revise SQL.", datetime(2026, 11, 9, 20, 0), "revise SQL",
                "weekly:MO", "20:00")
        self.ok("Remind me in 45 minutes to take a break.", datetime(2026, 11, 7, 13, 45), "take a break")
        # No time given: Nova asks instead of guessing, then finishes with the answer.
        asked = self.ask("Remind me on the 15th of every month to review expenses.", "What time")
        done = nr.answer(asked.clarify, "9 am", SAT)
        self.assertEqual((done.kind, done.title, done.schedule.rule, done.schedule.anchor, done.schedule.due),
                         ("ok", "review expenses", "monthly:15", "09:00", datetime(2026, 11, 15, 9, 0)))

    def test_relative_times(self):
        self.ok("remind me in an hour and a half to leave", datetime(2026, 11, 7, 14, 30), "leave")
        self.ok("remind me in half an hour to stretch", datetime(2026, 11, 7, 13, 30), "stretch")
        self.ok("remind me to call Ravi in 2 hours", datetime(2026, 11, 7, 15, 0), "call Ravi")
        self.ok("remind me in 2 days at 8am to check results", datetime(2026, 11, 9, 8, 0), "check results")
        self.ok("remind me in 1 week at 18:00 to water the plants", datetime(2026, 11, 14, 18, 0), "water the plants")
        with_seconds = parse("remind me in 10 minutes to stand", SAT.replace(second=40))
        self.assertEqual(with_seconds.schedule.due, datetime(2026, 11, 7, 13, 11), "rounded up, never early")

    def test_absolute_dates(self):
        self.ok("remind me on 25/12 at 10am to call grandma", datetime(2026, 12, 25, 10, 0), "call grandma")
        self.ok("set a reminder for 20 November at 4:30 pm to pick up parcel", datetime(2026, 11, 20, 16, 30),
                "pick up parcel")
        self.ok("remind me on 2026-12-01 at 07:15 to renew the domain", datetime(2026, 12, 1, 7, 15),
                "renew the domain")
        result = self.ok("remind me on 3 January at 9am to renew insurance", datetime(2027, 1, 3, 9, 0),
                         "renew insurance")
        self.assertEqual(result.schedule.notes, ["next year"])
        self.ok("remind me next friday at 3pm to submit timesheet", datetime(2026, 11, 13, 15, 0), "submit timesheet")
        self.ok("remind me to call mom tonight at 9", datetime(2026, 11, 7, 21, 0), "call mom")
        self.ok("remind me tomorrow morning at 7 to run", datetime(2026, 11, 8, 7, 0), "run")
        self.ok("remind me at 14:30 to call Ravi", datetime(2026, 11, 7, 14, 30), "call Ravi")
        self.ok("remind me at midnight to sleep", datetime(2026, 11, 8, 0, 0), "sleep")
        self.ok("remind me on the 20th at 6pm to pay the card bill", datetime(2026, 11, 20, 18, 0), "pay the card bill")

    def test_recurring_requests(self):
        self.ok("remind me to water plants every morning at 7", datetime(2026, 11, 8, 7, 0), "water plants",
                "daily", "07:00")
        self.ok("remind me every weekday at 9:30 am to stand up", datetime(2026, 11, 9, 9, 30), "stand up",
                "weekly:MO,TU,WE,TH,FR", "09:30")
        self.ok("remind me on Mondays and Thursdays at 7pm to go to the gym", datetime(2026, 11, 9, 19, 0),
                "go to the gym", "weekly:MO,TH", "19:00")
        self.ok("remind me every Saturday at 6 PM to call home", datetime(2026, 11, 7, 18, 0), "call home",
                "weekly:SA", "18:00")
        self.ok("remind me on the last day of every month at 9am to send invoices", datetime(2026, 11, 30, 9, 0),
                "send invoices", "monthly:last", "09:00")
        self.ok("remind me on the 31st of every month at 6pm to pay rent", datetime(2026, 11, 30, 18, 0), "pay rent",
                "monthly:31", "18:00")

    def test_ambiguity_is_asked_not_guessed(self):
        cases = {
            "remind me to call mom at 9": "AM or PM",
            "remind me at 12 to eat lunch": "noon or 12 midnight",
            "remind me on 05/06 at 10am to renew passport": "5 June or May 6",
            "remind me on saturday at 5pm to buy milk": "Today is Saturday",
            "remind me at 10am to stretch": "has already passed today",
            "remind me every week to back up": "Which day of the week",
            "remind me every month at 9am to pay bills": "which day of the month",
            "remind me tomorrow": "What should I remind you about",
            "remind me about the dentist": "When should I remind you",
            "remind me tomorrow to call the bank": "What time",
        }
        for text, fragment in cases.items():
            with self.subTest(text):
                self.ask(text, fragment)

    def test_answers_complete_the_request(self):
        def chain(text, reply):
            return nr.answer(parse(text).clarify, reply, SAT)

        self.assertEqual(chain("remind me to call mom at 9", "pm").schedule.due, datetime(2026, 11, 7, 21, 0))
        self.assertEqual(chain("remind me to call mom at 9", "9pm").schedule.due, datetime(2026, 11, 7, 21, 0))
        morning = chain("remind me to call mom at 9", "morning")
        self.assertEqual(morning.kind, "clarify", "9 AM has passed today, so Nova asks about tomorrow")
        self.assertEqual(nr.answer(morning.clarify, "yes", SAT).schedule.due, datetime(2026, 11, 8, 9, 0))
        self.assertEqual(chain("remind me at 12 to eat lunch", "noon").kind, "clarify")  # noon has passed today
        self.assertEqual(chain("remind me at 12 to sleep", "midnight").schedule.due, datetime(2026, 11, 8, 0, 0))
        self.assertEqual(chain("remind me on 05/06 at 10am to renew passport", "June").schedule.due,
                         datetime(2027, 6, 5, 10, 0))
        self.assertEqual(chain("remind me on saturday at 5pm to buy milk", "today").schedule.due,
                         datetime(2026, 11, 7, 17, 0))
        self.assertEqual(chain("remind me on saturday at 5pm to buy milk", "next week").schedule.due,
                         datetime(2026, 11, 14, 17, 0))
        weekly = chain("remind me every week to back up", "Fridays at 6pm")
        self.assertEqual((weekly.schedule.rule, weekly.schedule.anchor), ("weekly:FR", "18:00"))
        monthly = chain("remind me every month at 9am to pay bills", "the 1st")
        self.assertEqual((monthly.schedule.rule, monthly.schedule.due), ("monthly:1", datetime(2026, 12, 1, 9, 0)))
        self.assertEqual(chain("remind me tomorrow", "renew my library books").kind, "clarify")  # still needs a time
        self.assertEqual(chain("remind me about the dentist", "tomorrow at 4pm").schedule.due,
                         datetime(2026, 11, 8, 16, 0))
        self.assertEqual(chain("remind me to call mom at 9", "cancel").kind, "cancelled")
        self.assertIsNone(chain("remind me to call mom at 9", "what's the weather like?"),
                          "an unrelated reply isn't treated as an answer")

    def test_errors(self):
        for text, fragment in (
            ("remind me every other week to mow", "not on that schedule"),
            ("remind me every year on my birthday", "not on that schedule"),
            ("remind me on 2026-02-30 at 9am to x", "isn't a real date"),
            ("remind me on 31 November at 9am to x", "doesn't have a 31st"),
            ("remind me in 10 minutes at 5pm to x", "not both"),
            ("remind me today at 9:00 am to x", "Today at 9:00 AM is in the past"),
            ("remind me on 2025-01-01 at 09:00 to x", "in the past"),
        ):
            with self.subTest(text):
                result = parse(text)
                self.assertIn(result.kind, ("error", "clarify"))
                self.assertIn(fragment, result.message)

    def test_not_a_reminder(self):
        for text in ("what is a reminder?", "I need to remember milk", "remindme", "open notepad"):
            self.assertEqual(parse(text).kind, "none")


class RecurrenceTests(unittest.TestCase):
    def test_daily_and_weekly(self):
        self.assertEqual(nr.next_occurrence("daily", "09:00", datetime(2026, 11, 7, 8, 59)), datetime(2026, 11, 7, 9, 0))
        self.assertEqual(nr.next_occurrence("daily", "09:00", datetime(2026, 11, 7, 9, 0)), datetime(2026, 11, 8, 9, 0))
        rule = "weekly:MO,WE,FR"
        self.assertEqual(nr.next_occurrence(rule, "20:00", datetime(2026, 11, 7, 13)), datetime(2026, 11, 9, 20))
        self.assertEqual(nr.next_occurrence(rule, "20:00", datetime(2026, 11, 9, 20)), datetime(2026, 11, 11, 20))
        self.assertEqual(nr.next_occurrence("weekly:TH", "08:00", datetime(2026, 12, 31, 9)), datetime(2027, 1, 7, 8))

    def test_month_lengths_and_year_end(self):
        after = datetime(2027, 1, 31, 18, 0)
        chain = []
        for _ in range(5):
            after = nr.next_occurrence("monthly:31", "18:00", after)
            chain.append(after.date().isoformat())
        self.assertEqual(chain, ["2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31", "2027-06-30"])
        self.assertEqual(nr.next_occurrence("monthly:29", "09:00", datetime(2028, 1, 30)), datetime(2028, 2, 29, 9))
        self.assertEqual(nr.next_occurrence("monthly:last", "09:00", datetime(2026, 12, 31, 9)), datetime(2027, 1, 31, 9))
        self.assertEqual(nr.next_occurrence("monthly:15", "09:00", datetime(2026, 12, 20)), datetime(2027, 1, 15, 9))

    def test_rules_are_validated_and_described(self):
        for bad in ("hourly", "weekly:XX", "weekly:", "monthly:0", "monthly:32", "daily:2"):
            with self.subTest(bad), self.assertRaises(ValueError):
                nr.validate_rule(bad)
        self.assertEqual(nr.describe_rule("weekly:MO,TU,WE,TH,FR", "09:30"), "every weekday at 9:30 AM")
        self.assertEqual(nr.describe_rule("weekly:MO,TH", "19:00"), "every Monday and Thursday at 7:00 PM")
        self.assertIn("last day in shorter months", nr.describe_rule("monthly:31", "18:00"))


class DaylightSavingTests(unittest.TestCase):
    def test_relative_times_count_real_minutes(self):
        # Spring forward 29 Mar 2026: 01:30 + 60 real minutes = 03:30 (02:30 doesn't exist).
        self.assertEqual(nr.shift(datetime(2026, 3, 29, 1, 30), timedelta(minutes=60), TZ), datetime(2026, 3, 29, 3, 30))
        # Fall back 25 Oct 2026: 01:30 + 90 real minutes = 02:00 (the repeated hour).
        self.assertEqual(nr.shift(datetime(2026, 10, 25, 1, 30), timedelta(minutes=90), TZ), datetime(2026, 10, 25, 2, 0))
        result = nr.parse_reminder("remind me in 1 hour to check the oven", datetime(2026, 3, 29, 1, 30), TZ)
        self.assertEqual(result.schedule.due, datetime(2026, 3, 29, 3, 30))

    def test_recurring_time_of_day_is_kept(self):
        self.assertEqual(nr.next_occurrence("daily", "09:00", datetime(2026, 3, 28, 9, 0)), datetime(2026, 3, 29, 9, 0))
        self.assertEqual(nr.next_occurrence("daily", "09:00", datetime(2026, 10, 24, 9, 0)), datetime(2026, 10, 25, 9, 0))

    def test_elapsed_minutes(self):
        self.assertEqual(nr.elapsed_minutes(datetime(2026, 3, 29, 1, 30), datetime(2026, 3, 29, 3, 30), TZ), 60)
        self.assertLessEqual(nr.elapsed_minutes(datetime(2026, 3, 29, 2, 30), datetime(2026, 3, 29, 3, 0), TZ), 0)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_rem_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "nova_memory.db"
        patcher = mock.patch.object(assistant, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self):
        con = assistant.connect_db()
        self.addCleanup(con.close)
        return con

    def ready(self):
        assistant.initialize_db()
        import nova_gui
        nova_gui.initialize_productivity_tables()
        return self.connect()


LEGACY = """
CREATE TABLE nova_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, due_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, notified_at TEXT);
"""


class MigrationTests(StoreTest):
    def test_existing_reminders_survive_with_backup(self):
        con = sqlite3.connect(self.db)
        con.executescript(LEGACY)
        con.executemany("INSERT INTO nova_reminders (title, due_at, completed, created_at, notified_at) "
                        "VALUES (?, ?, ?, ?, ?)", [
                            ("call home", "2026-11-06 21:28", 0, "2026-11-06T21:08:58+05:30", "2026-11-06T21:28:00+05:30"),
                            ("pay rent", "2026-12-01 09:00", 0, "2026-11-06T21:10:35+05:30", None),
                            ("old", "2026-01-01 09:00", 1, "2025-12-01T09:00:00+05:30", "2026-01-01T09:00:00+05:30")])
        con.commit()
        con.close()
        con = self.ready()
        rows = con.execute("SELECT title, due_at, completed, notified_at, occurrence_at, recurrence FROM nova_reminders "
                           "ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ("call home", "2026-11-06 21:28", 0, "2026-11-06T21:28:00+05:30", "2026-11-06 21:28", None),
            ("pay rent", "2026-12-01 09:00", 0, None, "2026-12-01 09:00", None),
            ("old", "2026-01-01 09:00", 1, "2026-01-01T09:00:00+05:30", "2026-01-01 09:00", None)])
        self.assertEqual(len(list(self.tmp.glob("nova_memory.backup-*.db"))), 1)
        import nova_gui
        nova_gui.initialize_productivity_tables()
        self.assertEqual(len(list(self.tmp.glob("nova_memory.backup-*.db"))), 1, "repeat runs change nothing")
        # The already-alerted legacy reminder isn't shown again.
        self.assertEqual(nr.claim_due(con, SAT), [])
        groups = nr.grouped(con, SAT)
        self.assertEqual([r.title for r in groups["overdue"]], ["call home"])
        self.assertEqual([r.title for r in groups["completed"]], ["old"])


class StorageTests(StoreTest):
    def setUp(self):
        super().setUp()
        self.con = self.ready()

    def add(self, text, now=SAT):
        result = parse(text, now)
        self.assertEqual(result.kind, "ok", result.message)
        return nr.create(self.con, result.title, result.schedule, now)

    def test_duplicates_are_never_created_or_overwritten(self):
        first = self.add("remind me tomorrow at 9 AM to study")
        again = self.add("Remind me tomorrow at 9:00 am to STUDY")
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(again.reminder.id, first.reminder.id)
        self.assertIn("already have this reminder", nr.saved_message(again, SAT))
        other = self.add("remind me tomorrow at 10 AM to study")
        self.assertTrue(other.created)
        forced = nr.create(self.con, "study", first.reminder and nr.Schedule(first.reminder.due), allow_duplicate=True)
        self.assertTrue(forced.created)
        self.assertEqual(len(nr.active(self.con)), 3)

    def test_one_off_alerts_exactly_once(self):
        saved = self.add("remind me in 45 minutes to take a break").reminder
        self.assertEqual(nr.claim_due(self.con, SAT + timedelta(minutes=44)), [])
        (alert,) = nr.claim_due(self.con, SAT + timedelta(minutes=45))
        self.assertEqual((alert.reminder_id, alert.missed, alert.rule), (saved.id, False, None))
        self.assertEqual(nr.claim_due(self.con, SAT + timedelta(minutes=46)), [], "no second alert")
        self.assertEqual(nr.claim_due(self.con, SAT + timedelta(days=3)), [])

    def test_two_nova_windows_cannot_both_alert(self):
        self.add("remind me in 5 minutes to stretch")
        other = sqlite3.connect(self.db)
        try:
            first = nr.claim_due(self.con, SAT + timedelta(minutes=5))
            second = nr.claim_due(other, SAT + timedelta(minutes=5))
        finally:
            other.close()
        self.assertEqual((len(first), len(second)), (1, 0))

    def test_missed_after_restart(self):
        self.add("remind me tomorrow at 9 AM to study")
        restart = datetime(2026, 11, 9, 8, 0)   # Nova was closed on Sunday morning
        (alert,) = nr.claim_due(self.con, restart)
        self.assertTrue(alert.missed)
        self.assertIn("Missed while Nova wasn't running", nr.alert_lines(alert, restart)[0])
        self.assertEqual([r.title for r in nr.grouped(self.con, restart)["overdue"]], ["study"])

    def test_recurring_advances_and_counts_missed_occurrences(self):
        saved = self.add("remind me every day at 7am to take vitamins").reminder
        self.assertEqual(saved.due, datetime(2026, 11, 8, 7, 0))
        (alert,) = nr.claim_due(self.con, datetime(2026, 11, 8, 7, 0))
        self.assertEqual((alert.missed, alert.next_due), (False, datetime(2026, 11, 9, 7, 0)))
        self.assertEqual(nr.claim_due(self.con, datetime(2026, 11, 8, 7, 1)), [])
        # Nova closed for three days: one alert, not three.
        (alert,) = nr.claim_due(self.con, datetime(2026, 11, 11, 12, 0))
        self.assertEqual((alert.missed, alert.missed_count, alert.next_due), (True, 3, datetime(2026, 11, 12, 7, 0)))
        self.assertIn("Missed 3 times", nr.alert_lines(alert, datetime(2026, 11, 11, 12))[0])
        current = nr.get(self.con, saved.id)
        self.assertEqual((current.due, current.occurrence, current.notified_at), (datetime(2026, 11, 12, 7), datetime(2026, 11, 12, 7), None))
        kinds = [h[2] for h in nr.history(self.con, saved.id)]
        self.assertEqual(kinds.count("missed"), 2)

    def test_monthly_series_across_month_ends(self):
        saved = self.add("remind me on the 31st of every month at 6pm to pay rent",
                         now=datetime(2027, 1, 31, 19, 0)).reminder
        fired = []
        moment = datetime(2027, 1, 31, 19, 0)
        for _ in range(4):
            reminder = nr.get(self.con, saved.id)
            moment = reminder.due
            (alert,) = nr.claim_due(self.con, moment)
            fired.append(alert.occurrence.date().isoformat())
        self.assertEqual(fired, ["2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31"])

    def test_fall_back_hour_alerts_once(self):
        result = nr.parse_reminder("remind me on 2026-10-25 at 02:30 to check the boiler", datetime(2026, 10, 24, 12), TZ)
        nr.create(self.con, result.title, result.schedule)
        self.assertEqual(len(nr.claim_due(self.con, datetime(2026, 10, 25, 2, 30), tz=TZ)), 1)
        self.assertEqual(nr.claim_due(self.con, datetime(2026, 10, 25, 2, 30), tz=TZ), [], "second 02:30: no repeat")

    def test_spring_forward_gap_is_not_reported_as_missed(self):
        nr.create(self.con, "water plants", nr.Schedule(datetime(2026, 3, 29, 9, 0), "daily", "02:30"))
        self.con.execute("UPDATE nova_reminders SET due_at = '2026-03-29 02:30', occurrence_at = '2026-03-29 02:30'")
        self.con.commit()
        (alert,) = nr.claim_due(self.con, datetime(2026, 3, 29, 3, 0), tz=TZ)  # clocks jumped 02:00 -> 03:00
        self.assertFalse(alert.missed)
        self.assertEqual(alert.next_due, datetime(2026, 3, 30, 2, 30))

    def test_snooze_one_off_and_recurring(self):
        once = self.add("remind me in 5 minutes to stretch").reminder
        nr.claim_due(self.con, SAT + timedelta(minutes=5))
        snoozed = nr.snooze(self.con, once.id, 10, now=SAT + timedelta(minutes=6))
        self.assertEqual((snoozed.due, snoozed.occurrence, snoozed.notified_at),
                         (SAT + timedelta(minutes=16), SAT + timedelta(minutes=5), None))
        (again,) = nr.claim_due(self.con, SAT + timedelta(minutes=16))
        self.assertTrue(again.snoozed)
        self.assertIn("Snoozed reminder", nr.alert_lines(again, SAT + timedelta(minutes=16))[0])

        series = self.add("remind me every day at 2pm to drink water").reminder
        (alert,) = nr.claim_due(self.con, datetime(2026, 11, 7, 14, 0))
        child = nr.snooze(self.con, series.id, 30, now=datetime(2026, 11, 7, 14, 1), occurrence=alert.occurrence)
        self.assertEqual((child.parent_id, child.rule, child.due), (series.id, None, datetime(2026, 11, 7, 14, 31)))
        self.assertEqual(nr.get(self.con, series.id).due, datetime(2026, 11, 8, 14, 0), "the series keeps its schedule")
        with self.assertRaises(ValueError):
            nr.snooze(self.con, once.id, until=SAT - timedelta(hours=1), now=SAT)

    def test_complete_edit_skip_delete(self):
        once = self.add("remind me tomorrow at 9 AM to study").reminder
        self.assertTrue(nr.complete(self.con, once.id))
        self.assertTrue(nr.get(self.con, once.id).completed)
        self.assertEqual(nr.claim_due(self.con, datetime(2026, 11, 9)), [], "completed reminders never alert")

        series = self.add("remind me every Monday at 8 PM to revise SQL").reminder
        nr.complete(self.con, series.id, occurrence=series.occurrence)   # just this occurrence
        self.assertFalse(nr.get(self.con, series.id).completed)
        skipped = nr.skip_next(self.con, series.id)
        self.assertEqual(skipped.due, datetime(2026, 11, 16, 20, 0))
        edited = nr.update(self.con, series.id, "revise SQL joins",
                           nr.schedule_from_fields("2026-11-07", "19:30", "weekly", ["TU", "TH"], now=SAT), SAT)
        self.assertEqual((edited.title, edited.rule, edited.due), ("revise SQL joins", "weekly:TU,TH",
                                                                    datetime(2026, 11, 10, 19, 30)))
        nr.complete(self.con, series.id)   # end the series
        self.assertEqual([r.title for r in nr.grouped(self.con, SAT)["completed"]][:2], ["revise SQL joins", "study"])
        self.assertTrue(nr.delete(self.con, series.id))
        self.assertEqual(nr.history(self.con, series.id), [])
        with self.assertRaises(ValueError):
            nr.update(self.con, once.id, "study", nr.Schedule(SAT - timedelta(hours=1)), SAT)

    def test_schedule_from_fields(self):
        self.assertEqual(nr.schedule_from_fields("2026-11-08", "09:00", now=SAT).due, datetime(2026, 11, 8, 9))
        monthly = nr.schedule_from_fields("2026-11-07", "18:00", "monthly", month_day="last", now=SAT)
        self.assertEqual((monthly.rule, monthly.due), ("monthly:last", datetime(2026, 11, 30, 18)))
        daily = nr.schedule_from_fields("2026-12-01", "07:00", "daily", now=SAT)
        self.assertEqual(daily.due, datetime(2026, 12, 1, 7), "repeats start on the chosen date")
        for args, fragment in ((("2026-11-07", "9am"), "24-hour"), (("07/11/2026", "09:00"), "date like"),
                               (("2026-11-07", "09:00"), "in the past"),
                               (("2026-11-08", "09:00", "weekly", []), "at least one day"),
                               (("2026-11-08", "09:00", "monthly", (), "40"), "1 to 31")):
            with self.subTest(args), self.assertRaises(ValueError) as caught:
                nr.schedule_from_fields(*args, now=SAT)
            self.assertIn(fragment, str(caught.exception))

    def test_grouped_view(self):
        self.add("remind me in 30 minutes to stretch")
        self.add("remind me tomorrow at 9 AM to study")
        self.add("remind me next friday at 3pm to submit timesheet")
        self.add("remind me every weekday at 9:30 am to stand up")
        groups = nr.grouped(self.con, SAT)
        self.assertEqual({d.isoformat(): [r.title for r in rs] for d, rs in groups["days"].items()},
                         {"2026-11-07": ["stretch"], "2026-11-08": ["study"], "2026-11-13": ["submit timesheet"]})
        self.assertEqual([r.title for r in groups["recurring"]], ["stand up"])
        later = nr.grouped(self.con, SAT + timedelta(hours=2))
        self.assertEqual([r.title for r in later["overdue"]], ["stretch"])


class GuiGlueTests(StoreTest):
    @classmethod
    def setUpClass(cls):
        import nova_gui
        cls.gui = nova_gui

    def setUp(self):
        super().setUp()
        self.con = self.ready()

    def test_chat_flow_with_a_follow_up_question(self):
        reply, pending = self.gui.new_reminder_request("Remind me on the 15th of every month to review expenses.", SAT)
        self.assertIn("What time", reply)
        self.assertEqual(nr.active(self.con), [], "nothing saved until the question is answered")
        reply, pending = self.gui.answer_pending_reminder("9 AM", pending, SAT + timedelta(minutes=1))
        self.assertIsNone(pending)
        self.assertIn("Repeating reminder", reply)
        self.assertIn("on the 15th of every month at 9:00 AM", reply)
        self.assertIn("only alert you while Nova is running", reply)
        (saved,) = nr.active(self.con)
        self.assertEqual((saved.title, saved.rule, saved.due), ("review expenses", "monthly:15", datetime(2026, 11, 15, 9)))

    def test_stale_or_unrelated_answers_fall_through(self):
        _reply, pending = self.gui.new_reminder_request("remind me to call mom at 9", SAT)
        self.assertIsNone(self.gui.answer_pending_reminder("open notepad", pending, SAT))
        self.assertIsNone(self.gui.answer_pending_reminder("pm", pending, SAT + timedelta(minutes=30)), "expired")
        self.assertIsNone(self.gui.answer_pending_reminder("pm", object(), SAT), "garbage state is ignored")
        self.assertIsNone(self.gui.new_reminder_request("open notepad", SAT))

    def test_action_worker_routes_answers_first(self):
        import queue
        app = mock.Mock()
        app.events = queue.Queue()
        app.settings = {}
        app._pending_reminder = None
        app._finish_reminder_reply = lambda *a: self.gui.NovaGUI._finish_reminder_reply(app, *a)
        session = assistant.create_session()
        with mock.patch.object(self.gui, "datetime", wraps=datetime) as fake:
            fake.now.return_value = SAT
            self.gui.NovaGUI._action_worker(app, session, "remind me to call mom at 9", False)
            self.assertIsNotNone(app._pending_reminder)
            self.gui.NovaGUI._action_worker(app, session, "pm", False)
        self.assertIsNone(app._pending_reminder)
        (saved,) = nr.active(self.con)
        self.assertEqual(saved.due, datetime(2026, 11, 7, 21, 0))
        history = self.gui.fetch_messages(session)
        self.assertEqual([m[0] for m in history], ["user", "assistant", "user", "assistant"])
        self.assertIn("AM or PM", history[1][1])

    def test_task_runner_reminders_still_work(self):
        due = (datetime.now() + timedelta(hours=2)).replace(second=0, microsecond=0).strftime(nr.FMT)
        reminder_id = self.gui.save_reminder_db("Note: Standup", due)
        self.assertEqual(tuple(self.gui.get_reminder_db(reminder_id))[:3], (reminder_id, "Note: Standup", due))
        self.assertEqual(self.gui.save_reminder_db("Note: Standup", due), reminder_id, "no duplicate")

    def test_show_reminders_command(self):
        nr.create(self.con, "revise SQL", nr.Schedule(datetime(2026, 11, 9, 20), "weekly:MO", "20:00"))
        text = self.gui.handle_productivity_command("show my reminders")
        self.assertIn("revise SQL: every Monday at 8:00 PM", text)


if __name__ == "__main__":
    unittest.main()

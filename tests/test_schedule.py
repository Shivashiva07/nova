"""Tests for proactive assistance (Step 33): the shared scheduler (recurring, interval and one-time tasks),
exactly-once claiming across restarts, missed runs, daylight-saving transitions, retries and failure
notifications, pause/resume/edit/delete, permission enforcement through the agent engine, folder, app and
web-page watches, notifications with quiet hours and snooze, chat commands, and the window.

Time is controlled: every scheduler function takes `now`, so nothing waits for a real schedule.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_agent as na  # noqa: E402
import nova_reminders as nr  # noqa: E402
import nova_schedule as ns  # noqa: E402
import nova_settings  # noqa: E402
import nova_tasks as nt  # noqa: E402
from test_agent import AgentTest, FakeModel, plan, step  # noqa: E402
from test_reminders import EuropeTZ  # noqa: E402

NOW = datetime(2026, 11, 12, 8, 0)            # a Thursday
READ_ONLY_TOOLS = {"tools": ["search_files"], "max_safety": "read_only", "folders": []}


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="nova_schedule_"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        patcher = mock.patch.object(assistant, "DB_PATH", self.folder / "nova.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        assistant.initialize_db()
        self.con = assistant.connect_db()
        self.addCleanup(self.con.close)
        nt.initialize_schema(self.con)
        ns.ensure_schema(self.con)

    def daily(self, name="Morning check", at="09:00", **options):
        schedule = nr.Schedule(nr.next_occurrence("daily", at, NOW), "daily", at)
        return ns.create_job(self.con, name, "notify", "Check the invoices", "time", schedule=schedule, now=NOW,
                             **options)

    def runs(self, job):
        return ns.history(self.con, job.id, 100)


# ==================== the scheduler ====================
class SchedulerTests(ScheduleTest):
    def test_interval_rule_shares_the_reminder_engine(self):
        self.assertEqual(nr.next_occurrence("every:30", "09:00", datetime(2026, 11, 12, 10, 5)),
                         datetime(2026, 11, 12, 10, 30))
        self.assertEqual(nr.describe_rule("every:120", "09:00"), "every 2 hours")
        for bad in ("every:2", "every:x", "every:999999"):
            with self.subTest(bad), self.assertRaises(ValueError):
                nr.validate_rule(bad)

    def test_recurring_task_runs_once_per_occurrence(self):
        job = self.daily()
        self.assertEqual(ns.claim_due(self.con, now=datetime(2026, 11, 12, 8, 59)), [])
        claims = ns.claim_due(self.con, now=datetime(2026, 11, 12, 9, 0))
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].reason, "Scheduled for today at 9:00 AM")
        self.assertEqual(ns.claim_due(self.con, now=datetime(2026, 11, 12, 9, 0)), [], "claimed exactly once")
        ns.finish(self.con, claims[0], "succeeded", "ok", now=datetime(2026, 11, 12, 9, 1))
        job = ns.get_job(self.con, job.id)
        self.assertEqual(job.due, datetime(2026, 11, 13, 9, 0))
        self.assertEqual(job.last_status, "succeeded")
        run = self.runs(job)[0]
        self.assertEqual(run["started"], "2026-11-12 09:00:00")
        self.assertTrue(run["started_utc"].endswith("+00:00"), "execution times are also kept in UTC")
        self.assertIn("UTC", job.timezone)

    def test_one_time_task_is_done_after_running(self):
        schedule = nr.Schedule(datetime(2026, 11, 12, 10, 0))
        job = ns.create_job(self.con, "Once", "notify", "Hello", "time", schedule=schedule, now=NOW)
        with self.assertRaises(ns.ScheduleError):
            ns.create_job(self.con, "Past", "notify", "x", "time", schedule=nr.Schedule(NOW - timedelta(hours=1)),
                          now=NOW)
        claim = ns.claim_due(self.con, now=datetime(2026, 11, 12, 10, 0))[0]
        ns.finish(self.con, claim, "succeeded", now=datetime(2026, 11, 12, 10, 0))
        self.assertEqual(ns.get_job(self.con, job.id).status, "done")
        self.assertEqual(ns.claim_due(self.con, now=datetime(2026, 11, 13, 10, 0)), [])

    def test_no_duplicate_after_a_restart(self):
        job = self.daily()
        claim = ns.claim_due(self.con, now=datetime(2026, 11, 12, 9, 0))[0]
        # Nova is closed mid-run. At the next start the run is marked interrupted and retried once, never
        # run again as a fresh occurrence.
        self.con.close()
        con = assistant.connect_db()
        self.addCleanup(con.close)
        restart = datetime(2026, 11, 12, 9, 5)
        self.assertEqual(ns.recover(con, now=restart), 1)
        runs = ns.history(con, job.id)
        self.assertEqual(runs[0]["status"], "interrupted")
        self.assertEqual(ns.claim_due(con, now=restart), [], "the occurrence isn't simply run again")
        retry = ns.claim_due(con, now=datetime(2026, 11, 12, 9, 15))
        self.assertEqual([(c.occurrence, c.attempt) for c in retry], [(claim.occurrence, 2)])
        # Even if the same occurrence were found due again (e.g. a second Nova window), it can't start twice.
        with con:
            con.execute("UPDATE scheduled_jobs SET due_at = ?, claimed_at = NULL WHERE id = ?",
                        ("2026-11-12 09:00", job.id))
        self.assertEqual(ns.claim_due(con, now=datetime(2026, 11, 12, 9, 20)), [])
        self.assertEqual(ns.get_job(con, job.id).due, datetime(2026, 11, 13, 9, 0))

    def test_missed_runs_while_closed_or_asleep_run_once(self):
        job = self.daily(missed_window_hours=96)
        claims = ns.claim_due(self.con, now=datetime(2026, 11, 15, 10, 0))      # closed for three days
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].missed_count, 4)
        self.assertIn("Missed 4 time(s)", claims[0].reason)
        self.assertEqual(ns.get_job(self.con, job.id).due, datetime(2026, 11, 16, 9, 0), "next time is in the future")

    def test_missed_runs_can_be_skipped(self):
        skip = self.daily("Skipper", missed_policy="skip")
        old = self.daily("Too old", at="06:00", missed_window_hours=1)
        claims = ns.claim_due(self.con, now=datetime(2026, 11, 13, 9, 30))
        self.assertEqual(claims, [])
        self.assertEqual(self.runs(skip)[0]["status"], "missed")
        self.assertIn("skipped because its missed-run setting is “skip”", self.runs(skip)[0]["reason"])
        self.assertIn("more than 1 hours late", self.runs(old)[0]["reason"])
        notes = ns.notifications(self.con)
        self.assertEqual(sorted(n.title for n in notes), ["Skipped: Skipper", "Skipped: Too old"])
        self.assertEqual(ns.get_job(self.con, skip.id).due, datetime(2026, 11, 14, 9, 0))

    def test_daylight_saving_transitions(self):
        tz = EuropeTZ()
        start = datetime(2026, 3, 28, 12, 0)
        job = ns.create_job(self.con, "Night job", "notify", "x", "time",
                            schedule=nr.Schedule(datetime(2026, 3, 29, 2, 30), "daily", "02:30"), now=start)
        # Spring forward: 02:30 doesn't exist on 29 March; at 03:00 it's on time, not missed.
        claims = ns.claim_due(self.con, now=datetime(2026, 3, 29, 3, 0), tz=tz)
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0].reason.startswith("Scheduled"), claims[0].reason)
        ns.finish(self.con, claims[0], "succeeded", now=datetime(2026, 3, 29, 3, 0))
        # Fall back: 02:30 happens twice on 25 October; the task runs once.
        with self.con:
            self.con.execute("UPDATE scheduled_jobs SET due_at = '2026-10-25 02:30' WHERE id = ?", (job.id,))
        self.assertEqual(len(ns.claim_due(self.con, now=datetime(2026, 10, 25, 2, 30), tz=tz)), 1)
        ns.finish(self.con, ns.Claim(job, self.runs(job)[0]["id"], "2026-10-25 02:30", 1, ""), "succeeded",
                  now=datetime(2026, 10, 25, 2, 31))
        self.assertEqual(ns.claim_due(self.con, now=datetime(2026, 10, 25, 2, 30), tz=tz), [], "second 02:30")

    def test_retries_and_failure_notifications(self):
        schedule = nr.Schedule(datetime(2026, 11, 12, 10, 0))
        job = ns.create_job(self.con, "Flaky", "notify", "x", "time", schedule=schedule, now=NOW, max_retries=1,
                            retry_minutes=10)
        claim = ns.claim_due(self.con, now=datetime(2026, 11, 12, 10, 0))[0]
        note_id = ns.finish(self.con, claim, "failed", error="file in use", now=datetime(2026, 11, 12, 10, 1))
        note = ns.get_notification(self.con, note_id)
        self.assertEqual(note.title, "Failed: Flaky")
        self.assertIn("Problem: file in use", note.body)
        self.assertIn("Nova will try again today at 10:11 AM", note.body)
        self.assertEqual(note.reason, "Scheduled for today at 10:00 AM")
        self.assertEqual(ns.claim_due(self.con, now=datetime(2026, 11, 12, 10, 5)), [])
        retry = ns.claim_due(self.con, now=datetime(2026, 11, 12, 10, 11))[0]
        self.assertEqual(retry.attempt, 2)
        self.assertIn("Retry 1 of 1", retry.reason)
        ns.finish(self.con, retry, "failed", error="still in use", now=datetime(2026, 11, 12, 10, 12))
        job = ns.get_job(self.con, job.id)
        self.assertIsNone(job.retry_at, "no more retries")
        self.assertEqual(job.status, "done")

    def test_deferred_when_another_task_runs(self):
        job = self.daily()
        claim = ns.claim_due(self.con, now=datetime(2026, 11, 12, 9, 0))[0]
        ns.defer(self.con, claim, now=datetime(2026, 11, 12, 9, 0))
        self.assertEqual(self.runs(job), [], "the deferred attempt isn't counted")
        again = ns.claim_due(self.con, now=datetime(2026, 11, 12, 9, 2))
        self.assertEqual([(c.occurrence, c.attempt) for c in again], [("2026-11-12 09:00", 1)])

    def test_pause_resume_edit_delete(self):
        job = self.daily()
        ns.set_status(self.con, job.id, "paused", now=NOW)
        self.assertEqual(ns.claim_due(self.con, now=datetime(2026, 11, 14, 9, 0)), [])
        resumed = ns.set_status(self.con, job.id, "active", now=datetime(2026, 11, 14, 12, 0))
        self.assertEqual(resumed.due, datetime(2026, 11, 15, 9, 0), "no catch-up runs for the paused days")
        edited = ns.update_job(self.con, job.id, now=NOW, name="Evening check",
                               schedule=nr.Schedule(datetime(2026, 11, 14, 18, 0), "daily", "18:00"))
        self.assertEqual((edited.name, edited.anchor), ("Evening check", "18:00"))
        self.assertTrue(ns.delete_job(self.con, job.id))
        self.assertEqual(ns.list_jobs(self.con), [])

    def test_validation(self):
        with self.assertRaises(ns.ScheduleError):
            ns.create_job(self.con, "x", "agent", "find invoices", "time", schedule=nr.Schedule(NOW + timedelta(1)),
                          now=NOW)                                       # an agent task without approved permissions
        with self.assertRaises(ns.ScheduleError):
            ns.create_job(self.con, "x", "notify", "my password is hunter22", "time",
                          schedule=nr.Schedule(NOW + timedelta(1)), now=NOW)
        job = ns.create_job(self.con, "Agent", "agent", "find invoices in downloads", "time",
                            schedule=nr.Schedule(NOW + timedelta(1)), permissions=READ_ONLY_TOOLS, now=NOW)
        with self.assertRaises(ns.ScheduleError) as caught:
            ns.update_job(self.con, job.id, request="delete invoices in downloads")
        self.assertIn("approved again", str(caught.exception))


# ==================== watches ====================
class WatchTests(ScheduleTest):
    def setUp(self):
        super().setUp()
        # Watched folders live next to the tests: the system temp folder is inside AppData, which Nova
        # (rightly) refuses to watch.
        self.watch_root = Path(tempfile.mkdtemp(prefix="_agent_watch_", dir=Path(__file__).resolve().parent))
        self.addCleanup(shutil.rmtree, self.watch_root, True)

    def test_folder_watch_snapshot_and_cooldown(self):
        watched = self.watch_root / "Downloads"
        watched.mkdir()
        (watched / "old.pdf").write_text("x")
        job = ns.create_job(self.con, "PDFs", "notify", "New PDF", "file", {"folder": str(watched), "pattern": "*.pdf"},
                            now=NOW)
        self.assertIsNone(ns.check_file_job(self.con, job, now=NOW), "existing files don't count as new")
        (watched / "invoice.pdf").write_text("x")
        (watched / "notes.txt").write_text("x")
        (watched / "partial.pdf.crdownload").write_text("x")
        claim = ns.check_file_job(self.con, ns.get_job(self.con, job.id), now=NOW)
        self.assertEqual(claim.reason, f"Files changed in {watched}: invoice.pdf (created)")
        self.assertEqual(claim.files, [str(watched / "invoice.pdf")])
        ns.finish(self.con, claim, "succeeded", now=NOW)
        (watched / "second.pdf").write_text("x")
        self.assertIsNone(ns.check_file_job(self.con, ns.get_job(self.con, job.id), now=NOW + timedelta(seconds=20)),
                          "at most once a minute")
        with self.assertRaises(ns.ScheduleError):
            ns.create_job(self.con, "x", "notify", "x", "file", {"folder": str(self.watch_root / "missing")}, now=NOW)
        with self.assertRaises(ns.ScheduleError):                     # protected locations are refused
            ns.create_job(self.con, "x", "notify", "x", "file", {"folder": str(self.folder)}, now=NOW)

    def test_real_windows_change_notifications(self):
        watched = self.watch_root / "Live"
        watched.mkdir()
        fired = threading.Event()
        watcher = ns.FileWatcher(lambda job_id: fired.set(), debounce=0.2)
        self.addCleanup(watcher.stop)
        job = ns.create_job(self.con, "Live", "notify", "x", "file", {"folder": str(watched)}, now=NOW)
        watcher.refresh([job])
        time.sleep(0.3)
        (watched / "new.txt").write_text("x")
        self.assertTrue(fired.wait(5), "Windows reported the change (no polling)")

    def test_app_watch_fires_on_the_transition(self):
        opens = ns.create_job(self.con, "Notepad opened", "notify", "x", "app", {"app": "Notepad", "when": "opens"},
                              now=NOW)
        closes = ns.create_job(self.con, "Notepad closed", "notify", "x", "app", {"app": "notepad", "when": "closes"},
                               now=NOW)
        self.assertIsNone(ns.check_app_job(self.con, opens, False, now=NOW), "the first look only records the state")
        self.assertIsNone(ns.check_app_job(self.con, closes, False, now=NOW))
        claim = ns.check_app_job(self.con, ns.get_job(self.con, opens.id), True, now=NOW)
        self.assertEqual(claim.reason, "Notepad opened")
        self.assertIsNone(ns.check_app_job(self.con, ns.get_job(self.con, closes.id), True, now=NOW))
        claim = ns.check_app_job(self.con, ns.get_job(self.con, closes.id), False, now=NOW + timedelta(minutes=5))
        self.assertEqual(claim.reason, "Notepad closed")
        with self.assertRaises(ns.ScheduleError):
            ns.create_job(self.con, "x", "notify", "x", "app", {"app": "regedit"}, now=NOW)

    def test_web_watch_is_opt_in_limited_and_conditional(self):
        every = lambda m: nr.Schedule(nr.next_occurrence(f"every:{m}", "08:00", NOW), f"every:{m}", "08:00")
        for config, rule, problem in (({"url": "http://example.com"}, every(60), "https"),
                                      ({"url": "https://192.168.1.1/"}, every(60), "private"),
                                      ({"url": "https://example.com"}, every(30), "once every 60 minutes")):
            with self.subTest(problem), self.assertRaises(ns.ScheduleError) as caught:
                ns.create_job(self.con, "x", "notify", "x", "web", config, schedule=rule, now=NOW)
            self.assertIn(problem, str(caught.exception))
        job = ns.create_job(self.con, "Prices", "notify", "The page changed", "web", {"url": "https://example.com/p"},
                            schedule=every(60), now=NOW)
        pages = iter([(200, "<p>Price 10</p>", '"a"', None), (304, None, '"a"', None), (200, "<p>Price 12</p>", '"b"', None)])
        requests = []

        def fetch(url, etag, modified):
            requests.append((url, etag))
            return next(pages)

        with self.assertRaises(ns.ScheduleError):
            ns.check_web_job(self.con, job, {"online_enabled": False}, fetch)
        self.assertEqual(requests, [], "nothing is sent while online integrations are off")
        settings = {"online_enabled": True}
        self.assertEqual(ns.check_web_job(self.con, ns.get_job(self.con, job.id), settings, fetch)[0], "unchanged")
        self.assertEqual(ns.check_web_job(self.con, ns.get_job(self.con, job.id), settings, fetch)[0], "unchanged")
        self.assertEqual(requests[1], ("https://example.com/p", '"a"'), "a conditional request")
        self.assertEqual(ns.check_web_job(self.con, ns.get_job(self.con, job.id), settings, fetch)[0], "changed")


# ==================== notifications ====================
class NotificationTests(ScheduleTest):
    def test_quiet_hours_across_midnight(self):
        settings = {"quiet_hours_enabled": True, "quiet_start": "22:00", "quiet_end": "07:00"}
        for hour, quiet in ((21, False), (22, True), (2, True), (6, True), (7, False), (12, False)):
            self.assertEqual(ns.in_quiet_hours(settings, datetime(2026, 11, 12, hour, 30)), quiet, hour)
        self.assertFalse(ns.in_quiet_hours(dict(settings, quiet_hours_enabled=False), datetime(2026, 11, 12, 23)))

    def test_delivery_once_quiet_hours_and_snooze(self):
        settings = {"quiet_hours_enabled": True, "quiet_start": "22:00", "quiet_end": "07:00"}
        note = ns.notify(self.con, "Done: backup", "ok", "Scheduled for today at 11:00 PM", now=datetime(2026, 11, 12, 23))
        self.assertEqual(ns.due_for_delivery(self.con, settings, datetime(2026, 11, 12, 23, 0)), [])
        self.assertTrue(ns.get_notification(self.con, note).quiet, "kept silently in the inbox")
        delivered = ns.due_for_delivery(self.con, settings, datetime(2026, 11, 13, 7, 0))
        self.assertEqual([n.id for n in delivered], [note])
        self.assertEqual(ns.due_for_delivery(self.con, settings, datetime(2026, 11, 13, 7, 1)), [], "delivered once")
        ns.set_notification(self.con, note, "snoozed", 60, now=datetime(2026, 11, 13, 7, 2))
        self.assertEqual(ns.due_for_delivery(self.con, settings, datetime(2026, 11, 13, 7, 30)), [])
        self.assertEqual(len(ns.due_for_delivery(self.con, settings, datetime(2026, 11, 13, 8, 3))), 1)
        ns.set_notification(self.con, note, "dismissed")
        self.assertEqual(ns.notifications(self.con), [])
        self.assertEqual(len(ns.notifications(self.con, include_dismissed=True)), 1)

    def test_notification_preferences(self):
        quiet_job = self.daily("Quiet", notify="failures")
        claim = ns.claim_due(self.con, now=datetime(2026, 11, 12, 9, 0))[0]
        self.assertIsNone(ns.finish(self.con, claim, "succeeded", now=datetime(2026, 11, 12, 9, 0)))
        never = self.daily("Never", notify="never", at="10:00")
        claim = ns.claim_due(self.con, now=datetime(2026, 11, 12, 10, 0))[0]
        self.assertIsNone(ns.finish(self.con, claim, "failed", error="x", now=datetime(2026, 11, 12, 10, 0)))
        self.assertTrue(quiet_job and never)


class CommandTests(unittest.TestCase):
    def test_parse_commands(self):
        draft = ns.parse_command("schedule every weekday at 9am: find invoices in downloads", now=NOW)
        self.assertEqual((draft.action, draft.request, draft.trigger), ("agent", "find invoices in downloads", "time"))
        self.assertEqual((draft.schedule.rule, draft.schedule.anchor), ("weekly:MO,TU,WE,TH,FR", "09:00"))
        draft = ns.parse_command("schedule tomorrow at 18:00: remind me to back up the laptop", now=NOW)
        self.assertEqual((draft.action, draft.request), ("notify", "back up the laptop"))
        self.assertEqual(draft.schedule.due, datetime(2026, 11, 13, 18, 0))
        draft = ns.parse_command("watch downloads for pdf: read_document {files}")
        self.assertEqual((draft.trigger, draft.config["pattern"], draft.action), ("file", "*.pdf", "agent"))
        draft = ns.parse_command("notify me when notepad closes")
        self.assertEqual((draft.trigger, draft.config), ("app", {"app": "notepad", "when": "closes"}))
        self.assertEqual(ns.parse_command("list my automations"), "list")
        for text in ("remind me in 10 minutes to stretch", "what's the schedule for today?", "watch a movie"):
            self.assertIsNone(ns.parse_command(text), text)

    def test_settings(self):
        clean, warnings = nova_settings.validate_settings({"quiet_start": "25:00", "quiet_end": "06:30",
                                                           "automation_enabled": "false"})
        self.assertEqual((clean["quiet_start"], clean["quiet_end"], clean["automation_enabled"]),
                         ("22:00", "06:30", False))
        self.assertTrue(warnings)


# ==================== permissions, through the agent engine ====================
class PermissionTests(AgentTest):
    """Scheduled agent runs through the window's real runner, with a scripted model and fake services."""

    def setUp(self):
        super().setUp()
        import nova_gui
        self.gui = nova_gui
        self.con = assistant.connect_db()
        self.addCleanup(self.con.close)
        ns.ensure_schema(self.con)
        self.asks = []
        self.app = mock.Mock()
        self.app.settings = {}
        self.app.task_runner = mock.Mock(services=self.services, _lock=threading.Lock())
        self.app.agent_registry = na.default_registry()
        self.app._scheduled_cancels = set()
        self.app.events = mock.Mock()
        self.app.desktop = self.app.browser = self.app.vision = None
        self.app._agent_ask = lambda kind, payload, timeout=600: self.asks.append(kind) or False   # nobody answers
        for p in (mock.patch.object(nova_gui, "model_capabilities", return_value=["completion"]),
                  mock.patch.object(nova_gui, "model_context_max", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def job(self, request, permissions):
        job = ns.create_job(self.con, "Task", "agent", request, "time", schedule=nr.Schedule(NOW + timedelta(1)),
                            permissions=permissions, now=NOW)
        return ns.fire_event(self.con, job, "test run", now=NOW)

    def run_claim(self, claim, model):
        with mock.patch.object(self.gui.ollama, "chat", model):
            return self.gui.NovaGUI._run_scheduled_agent(self.app, claim)

    def test_permissions_from_the_checked_plan(self):
        p = na.AgentPlan("x", [na.AgentStep("s1", "search_files", {"term": "a", "folder": str(self.downloads)},
                                            safety=na.READ_ONLY),
                               na.AgentStep("s2", "copy_file", {"file": "$s1.matches[0]", "destination": str(self.allowed)},
                                            safety=na.REVERSIBLE)])
        perms = ns.permissions_for(p)
        self.assertEqual(perms, {"tools": ["copy_file", "search_files"], "max_safety": "reversible",
                                 "folders": sorted([str(self.downloads), str(self.allowed)])})
        self.assertIsNone(ns.plan_allowed(p, perms))
        self.assertIn("recycle file", ns.plan_allowed(na.AgentPlan("x", [na.AgentStep(
            "s1", "recycle_file", {}, safety=na.CONSEQUENTIAL)]), perms))
        self.assertTrue(ns.folder_allowed([str(self.allowed / "Sub")], perms))
        self.assertFalse(ns.folder_allowed([str(self.root)], perms))

    def test_an_approved_task_runs_with_the_same_engine(self):
        claim = self.job("find invoice files in downloads", READ_ONLY_TOOLS)
        model = FakeModel(plan(step("s1", "search_files", {"term": "invoice", "folder": "downloads"})))
        status, summary, error, run_id = self.run_claim(claim, model)
        self.assertEqual(status, "succeeded", summary)
        self.assertIsNotNone(run_id, "recorded in the task history like any agent task")
        self.assertEqual(nt.list_runs(self.con)[0]["planner"], "agent")

    def test_a_plan_that_needs_more_than_approved_does_not_run(self):
        claim = self.job("look through downloads for my invoices and keep them safe", READ_ONLY_TOOLS)
        model = FakeModel(plan(step("s1", "search_files", {"term": "invoice", "folder": "downloads"}),
                               step("s2", "copy_file", {"file": "$s1.matches[0]", "destination": str(self.allowed)})))
        status, summary, error, _run = self.run_claim(claim, model)
        self.assertEqual(status, "needs_review")
        self.assertIn("copy file, which you didn't approve", error)
        self.assertFalse((self.allowed / "invoice-2026.pdf").exists(), "nothing ran")
        ns.finish(self.con, claim, status, summary, error, now=NOW)
        self.assertEqual(ns.get_job(self.con, claim.job.id).status, "needs_review")
        self.assertEqual(ns.notifications(self.con)[0].title, "Needs your review: Task")

    def test_consequential_steps_still_need_a_person(self):
        target = self.allowed / "notes.txt"
        claim = self.job(f"recycle {target}", {"tools": ["recycle_file"], "max_safety": "consequential",
                                               "folders": [str(self.allowed)]})
        model = FakeModel(plan(step("s1", "recycle_file", {"file": str(target)})))
        status, _summary, _error, _run = self.run_claim(claim, model)
        self.assertEqual(self.asks, ["step"], "asked right before, like an interactive task")
        self.assertEqual(status, "cancelled")
        self.assertTrue(target.exists(), "no answer means no")

    def test_folders_are_not_expanded(self):
        other = self.root / "Other"
        other.mkdir()
        claim = self.job("create a file in other", {"tools": ["create_file"], "max_safety": "reversible",
                                                    "folders": [str(self.allowed)]})
        model = FakeModel(plan(step("s1", "create_file", {"folder": str(other), "name": "a.txt", "content": "x"})))
        status, _summary, _error, _run = self.run_claim(claim, model)
        self.assertNotEqual(status, "succeeded")
        self.assertFalse((other / "a.txt").exists())

    def test_busy_means_try_again_shortly(self):
        claim = self.job("find invoice files in downloads", READ_ONLY_TOOLS)
        self.app.task_runner._lock.acquire()
        self.addCleanup(self.app.task_runner._lock.release)
        self.assertIsNone(self.run_claim(claim, FakeModel()))


# ==================== the window ====================
class WindowTests(AgentTest):
    def setUp(self):
        super().setUp()
        import customtkinter as ctk
        import gc
        import nova_gui
        self.gui = nova_gui
        for p in (mock.patch.object(nova_gui, "SETTINGS_PATH", self.db / "settings.json"),
                  mock.patch.object(nova_gui, "BACKUP_DIR", self.db / "backups"),
                  mock.patch.object(nova_gui, "load_tray_dependencies", return_value=None),
                  mock.patch.object(nova_gui, "model_capabilities", return_value=["completion"]),
                  mock.patch.object(nova_gui, "model_context_max", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        nova_gui.initialize_document_tables()
        nova_gui.initialize_context_tables()
        gc.collect()
        try:
            self.tk = ctk.CTk()
        except Exception as exc:
            self.skipTest(f"no display: {exc}")
        self.tk.withdraw()
        self.app = nova_gui.NovaGUI(self.tk)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.app.exit_application(force=True)
            self.tk.update()
        except Exception:
            pass
        import gc
        gc.collect()

    def _walk(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk(child)

    def pump(self, condition, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not condition():
            self.tk.update()
            time.sleep(0.02)
        self.tk.update()

    def test_a_due_task_runs_in_the_scheduler_check_and_notifies(self):
        con = assistant.connect_db()
        self.addCleanup(con.close)
        job = ns.create_job(con, "Stretch", "notify", "Time to stretch", "time",
                            schedule=nr.Schedule(datetime.now() + timedelta(hours=1)))
        with con:                                             # its time has come
            con.execute("UPDATE scheduled_jobs SET due_at = ? WHERE id = ?",
                        (nr.fmt(datetime.now().replace(second=0, microsecond=0)), job.id))
        self.app._poll_automations()
        self.pump(lambda: ns.notifications(con))
        note = ns.notifications(con)[0]
        self.assertEqual((note.title, note.body), ("Stretch", "Time to stretch"))
        self.assertTrue(note.reason.startswith("Scheduled for today"))
        self.app._poll_automations()                          # delivered: a notification window
        self.tk.update()
        self.assertTrue(any(w.winfo_exists() for w in self.app._note_windows))
        self.assertEqual(ns.get_job(con, job.id).status, "done")

    def test_paused_automations_run_nothing(self):
        con = assistant.connect_db()
        self.addCleanup(con.close)
        job = ns.create_job(con, "x", "notify", "x", "time", schedule=nr.Schedule(datetime.now() + timedelta(hours=1)))
        with con:
            con.execute("UPDATE scheduled_jobs SET due_at = ? WHERE id = ?",
                        (nr.fmt(datetime.now().replace(second=0, microsecond=0)), job.id))
        self.app.settings["automation_enabled"] = False
        self.app._poll_automations()
        self.assertEqual(ns.history(con, job.id), [])

    def test_automations_window_lists_tasks(self):
        con = assistant.connect_db()
        self.addCleanup(con.close)
        ns.create_job(con, "Morning invoices", "notify", "Check invoices", "time",
                      schedule=nr.Schedule(datetime.now() + timedelta(days=1), "daily", "09:00"))
        self.app.open_automations()
        self.tk.update()
        labels = [w.cget("text") for w in self._walk(self.app._automations_window) if w.__class__.__name__ == "CTkLabel"]
        self.assertIn("Morning invoices", labels)
        self.assertTrue(any("Every day at 9:00 AM" in t for t in labels))


if __name__ == "__main__":
    unittest.main()

"""Tests for safe multi-step task execution (nova_tasks + nova_gui glue).

Run from the project folder:  python -m unittest discover -s tests -v

Nothing real is launched: every test uses fake services that record what
would have happened. The task history goes to a temporary database.
"""
import json
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_tasks as nt  # noqa: E402


class FakeProcess:
    def __init__(self, code=None, pid=4242):
        self.code, self.pid = code, pid

    def poll(self):
        return self.code


class Fakes:
    """Records every operation instead of performing it."""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        for name in nt.KNOWN_FOLDERS:
            (self.home / name.title()).mkdir(parents=True, exist_ok=True)
        (self.home / "Downloads" / "invoice-2026.pdf").write_text("x")
        (self.home / "Projects" / "nova").mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.code_exe = self.root / "VS Code" / "Code.exe"
        self.code_exe.parent.mkdir()
        self.code_exe.write_text("")
        self.launched, self.urls, self.folders = [], [], []
        self.notes, self.reminders = {}, {}
        self.exit_code = None
        self.launch_error = None
        self.browser_ok = True
        self.search_delay = 0
        self.corrupt_notes = False

    def launch(self, argv):
        if self.launch_error:
            raise self.launch_error
        self.launched.append(list(argv))
        return FakeProcess(self.exit_code)

    def open_url(self, url):
        self.urls.append(url)
        return self.browser_ok

    def search(self, root, term, seconds, should_stop):
        end = time.monotonic() + self.search_delay
        while time.monotonic() < end:
            if should_stop():
                return [], True
            time.sleep(0.01)
        return [p for p in Path(root).rglob("*") if term.lower() in p.name.lower()], False

    def save_note(self, title, content):
        note_id = len(self.notes) + 1
        self.notes[note_id] = (note_id, title, content)
        return note_id

    def get_note(self, note_id):
        return None if self.corrupt_notes else self.notes.get(note_id)

    def save_reminder(self, title, due):
        reminder_id = len(self.reminders) + 1
        self.reminders[reminder_id] = (reminder_id, title, due, 0)
        return reminder_id

    def services(self):
        return nt.Services(
            open_url=self.open_url, launch=self.launch, open_folder=self.folders.append,
            find_vscode=lambda: str(self.code_exe), search_files=self.search,
            save_note=self.save_note, get_note=self.get_note, save_reminder=self.save_reminder,
            get_reminder=self.reminders.get,
            known_folders={n: self.home / n.title() for n in nt.KNOWN_FOLDERS}, home=self.home,
        )


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_tasks_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        quick = mock.patch.object(nt, "LAUNCH_CHECK_SECONDS", 0.05)
        quick.start()
        self.addCleanup(quick.stop)
        patcher = mock.patch.object(assistant, "DB_PATH", self.tmp / "nova.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        assistant.initialize_db()
        con = assistant.connect_db()
        nt.initialize_schema(con)
        con.close()
        self.fakes = Fakes(self.tmp)
        self.services = self.fakes.services()
        self.updates = []
        self.runner = nt.TaskRunner(assistant.connect_db, self.services, on_update=lambda *a: self.updates.append(a))

    def plan(self, text, chat=None):
        return nt.plan_request(text, self.services, chat=chat, model="test-model")

    def run_plan(self, plan, confirm=lambda step: True, cancel=None, timeouts=None):
        con = assistant.connect_db()
        try:
            run_id = nt.create_run(con, plan)
        finally:
            con.close()
        status, summary = self.runner.run(run_id, plan, cancel or threading.Event(), confirm, timeouts)
        return run_id, status, summary

    def history(self):
        con = assistant.connect_db()
        try:
            return nt.list_runs(con)
        finally:
            con.close()

    def actions(self, plan):
        return [(s.action, s.params) for s in plan.steps]


class PlannerTests(TaskTest):
    def test_examples_from_the_brief(self):
        vscode = self.plan("Open VS Code and then open a selected project folder")
        self.assertEqual(self.actions(vscode), [("open_app", {"app": "vscode"}),
                                                ("open_folder_in_vscode", {"folder": nt.ASK})])
        note = self.plan("Create a note called Groceries saying milk and eggs and remind me about it in 30 minutes")
        self.assertEqual(self.actions(note), [("create_note", {"title": "Groceries", "content": "milk and eggs"}),
                                              ("schedule_reminder", {"title": "Note: Groceries", "in_minutes": 30})])
        search = self.plan("open my downloads folder and search it for invoice")
        self.assertEqual(self.actions(search), [("open_folder", {"folder": "downloads"}),
                                                ("search_files", {"term": "invoice", "folder": "downloads"})])
        apps = self.plan("open notepad, calculator and paint")
        self.assertEqual([p["app"] for _a, p in self.actions(apps)], ["notepad", "calculator", "paint"])
        self.assertTrue(all(p.runnable for p in (vscode, note, search, apps)))
        self.assertEqual(vscode.planner, "rules")

    def test_descriptions_are_readable(self):
        plan = self.plan("open youtube and then remind me to stretch in 2 hours")
        self.assertEqual([s.description for s in plan.steps],
                         ["Open YouTube in your browser", "Remind you “stretch” in 2 hours"])

    def test_single_commands_and_normal_chat_are_not_tasks(self):
        for text in ("open notepad", "remind me to call home in 20 minutes", "search my documents for invoice",
                     "find a recipe and remind me in 30 minutes", "open the window and search for the light switch",
                     "What's the weather and should I open an umbrella?", "note: Shopping | eggs and milk"):
            with self.subTest(text):
                self.assertIsNone(self.plan(text))

    def test_explicit_single_step_task(self):
        plan = self.plan("task: open notepad")
        self.assertEqual(self.actions(plan), [("open_app", {"app": "notepad"})])

    def test_forbidden_requests_are_refused_without_asking_the_model(self):
        chat = mock.Mock(side_effect=AssertionError("the model must not be consulted"))
        for text in ("task: open notepad and delete my temp files", "task: run powershell Get-Process",
                     "task: edit the registry to disable updates", "task: open notepad and then type my password",
                     "task: install python 3.12", "task: open C:\\Windows\\System32\\cmd.exe",
                     "task: move my downloads to the desktop", "task: give yourself administrator rights"):
            with self.subTest(text):
                plan = self.plan(text, chat=chat)
                self.assertFalse(plan.runnable)
                self.assertTrue(any(p.startswith(nt.FORBIDDEN_PREFIX) for p in plan.problems), plan.problems)
        chat.assert_not_called()

    def test_folder_outside_home_needs_extra_confirmation(self):
        plan = self.plan(f"open the folder {self.fakes.outside} and open notepad")
        self.assertIn("outside your user folder", plan.steps[0].sensitive)
        inside = self.plan(f"open the folder {self.fakes.home / 'Projects' / 'nova'} in vs code and open notepad")
        self.assertIsNone(inside.steps[0].sensitive)


class ValidationTests(TaskTest):
    def check(self, action, params, fragment, planner="rules"):
        plan = nt.validate_plan(nt.Plan("x", [nt.Step(action, params)], planner), self.services)
        self.assertFalse(plan.runnable)
        self.assertIn(fragment, " ".join(plan.problems))

    def test_rejections(self):
        exe = self.tmp / "evil.exe"
        exe.write_text("")
        cases = [
            ("run_command", {"command": "del /s C:\\"}, "isn't an action"),
            ("open_app", {"app": "notepad", "args": "/c calc"}, "unexpected detail"),
            ("open_app", {"app": "cmd"}, "can't be started"),
            ("open_app", {"app": "task manager"}, "can't be started"),
            ("open_app", {"app": "notepad.exe & del *"}, "isn't a supported app"),
            ("open_website", {"site": "evil.example"}, "isn't a supported website"),
            ("open_folder", {"folder": "\\\\server\\share"}, "Network"),
            ("open_folder", {"folder": "//server/share"}, "Network"),
            ("open_folder", {"folder": "Projects"}, "full folder path"),
            ("open_folder", {"folder": str(exe)}, "is a file, not a folder"),
            ("open_folder", {"folder": str(self.tmp / "nope")}, "doesn't exist"),
            ("open_folder", {"folder": "C:\\x:stream"}, "valid folder"),
            ("search_files", {"term": "..\\..\\secret", "folder": "documents"}, "part of a file name"),
            ("create_note", {"title": ""}, "title is missing"),
            ("create_note", {"title": "x" * 500, "content": ""}, "longer than"),
            ("create_note", {"title": "a\x00b"}, "control characters"),
            ("schedule_reminder", {"title": "x"}, "either a delay"),
            ("schedule_reminder", {"title": "x", "in_minutes": 5, "at": "2030-01-01 10:00"}, "either a delay"),
            ("schedule_reminder", {"title": "x", "in_minutes": 0}, "between 1 minute"),
            ("schedule_reminder", {"title": "x", "in_minutes": True}, "number of minutes"),
            ("schedule_reminder", {"title": "x", "at": "2001-01-01 10:00"}, "in the past"),
            ("schedule_reminder", {"title": "x", "at": "tomorrow"}, "YYYY-MM-DD"),
        ]
        for action, params, fragment in cases:
            with self.subTest(action=action, params=params):
                self.check(action, params, fragment)

    def test_too_many_steps(self):
        steps = [nt.Step("open_app", {"app": "notepad"}) for _ in range(nt.MAX_STEPS + 1)]
        plan = nt.validate_plan(nt.Plan("x", steps), self.services)
        self.assertIn("at most", " ".join(plan.problems))

    def test_choose_folder_is_required_before_running(self):
        plan = nt.validate_plan(nt.Plan("x", [nt.Step("open_folder", {"folder": nt.ASK})]), self.services)
        self.assertTrue(plan.runnable)
        self.assertEqual(plan.needs_folder(), [0])
        nt.validate_plan(plan, self.services, allow_ask=False)
        self.assertFalse(plan.runnable)


class ModelPlannerTests(TaskTest):
    def model(self, payload):
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return mock.Mock(return_value={"message": {"content": content}})

    def test_valid_model_plan_is_validated_and_marked(self):
        chat = self.model({"steps": [
            {"action": "open_app", "params": {"app": "VS Code"}},
            {"action": "open_folder_in_vscode", "params": {"folder": str(self.fakes.home / "Projects" / "nova")}},
        ], "unsupported": []})
        plan = self.plan("task: get me set up to work on nova", chat=chat)
        self.assertTrue(plan.runnable)
        self.assertEqual(plan.planner, "model")
        self.assertEqual(plan.steps[0].params, {"app": "vscode"})
        self.assertIn("suggested by the AI model", plan.steps[1].sensitive)
        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["format"], "json")
        self.assertIn("open_folder_in_vscode(folder:", kwargs["messages"][0]["content"])

    def test_model_cannot_add_commands_fields_or_flags(self):
        attempts = [
            {"steps": [{"action": "run_powershell", "params": {"script": "Remove-Item -Recurse C:\\"}}]},
            {"steps": [{"action": "open_app", "params": {"app": "powershell -c rm -r ~"}}]},
            {"steps": [{"action": "open_app", "params": {"app": "notepad"}, "command": "calc.exe"}]},
            {"steps": [{"action": "open_app", "params": {"app": "notepad", "path": "C:\\evil.exe"}}]},
            {"steps": [{"action": "open_app", "params": {"app": "notepad"}, "sensitive": False}]},
            {"steps": [{"action": "open_folder", "params": {"folder": "C:\\Windows\\System32\\cmd.exe"}}]},
            {"steps": [{"action": "open_website", "params": {"site": "https://evil.example/payload"}}]},
            {"steps": [{"action": "open_app", "params": {"app": "notepad"}}],
             "unsupported": ["delete the temp folder"]},
            {"steps": "open notepad"},
            "rm -rf / ; not json",
            {"steps": []},
        ]
        for payload in attempts:
            with self.subTest(payload=str(payload)[:60]):
                plan = self.plan("task: do something clever", chat=self.model(payload))
                self.assertFalse(plan.runnable, plan.problems)
                _id, status, summary = self.run_plan(plan)  # even if forced into the runner...
                self.assertEqual(status, "rejected")         # ...no step runs
                self.assertIn("didn't pass Nova's safety checks", summary)
        self.assertEqual(self.fakes.launched, [])
        self.assertEqual(self.fakes.urls, [])

    def test_placeholder_unsupported_entries_are_ignored(self):
        chat = self.model({"steps": [{"action": "open_app", "params": {"app": "notepad"}}],
                           "unsupported": ["...", "<parts of the request none of the actions can do>", "none", ""]})
        self.assertTrue(self.plan("task: bring up a text editor", chat=chat).runnable)

    def test_model_unreachable(self):
        plan = self.plan("task: get me ready for work", chat=mock.Mock(side_effect=ConnectionError("refused")))
        self.assertFalse(plan.runnable)
        self.assertIn("couldn't be reached", plan.problems[0])


class RunnerTests(TaskTest):
    def test_successful_run_is_verified_and_recorded(self):
        plan = self.plan("open notepad and then open my downloads folder and search it for invoice")
        run_id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded")
        self.assertEqual(self.fakes.launched, [["notepad.exe"]])
        self.assertEqual(self.fakes.folders, [str(self.fakes.home / "Downloads")])
        self.assertIn("invoice-2026.pdf", plan.steps[2].result)
        self.assertIn("**Task complete**", summary)
        (run,) = self.history()
        self.assertEqual((run["id"], run["status"], run["planner"]), (run_id, "succeeded", "rules"))
        self.assertEqual([s["status"] for s in run["steps"]], ["succeeded"] * 3)
        self.assertTrue(run["started_at"] and run["finished_at"])
        self.assertIn(("running",), [(u[2],) for u in self.updates])

    def test_note_then_reminder_verified_in_order(self):
        plan = self.plan("note: Standup | prepare demo and remind me about it in 15 minutes")
        _run_id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded")
        due = datetime.strptime(self.fakes.reminders[1][2], "%Y-%m-%d %H:%M")
        self.assertAlmostEqual((due - datetime.now()).total_seconds() / 60, 15, delta=1.1)
        self.assertEqual(self.fakes.reminders[1][1], "Note: Standup")

    def test_failed_step_stops_dependent_steps(self):
        self.fakes.corrupt_notes = True  # the note can't be read back, so it doesn't count as saved
        plan = self.plan("note: Standup | prepare demo and remind me about it in 15 minutes")
        _run_id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "failed")
        self.assertEqual([s.status for s in plan.steps], ["failed", "skipped"])
        self.assertEqual(self.fakes.reminders, {}, "the dependent reminder was never created")
        self.assertIn("couldn't be confirmed", plan.steps[0].error)
        self.assertIn("**Task stopped**", summary)

    def test_launch_failures_are_failures(self):
        for error, code, fragment in ((FileNotFoundError(), None, "isn't installed"),
                                      (OSError(740, "elevation"), None, "administrator"),
                                      (None, 1, "exit code 1")):
            with self.subTest(fragment):
                self.fakes.launch_error, self.fakes.exit_code = error, code
                if error is not None and getattr(error, "errno", None) == 740:
                    error.winerror = 740
                plan = self.plan("open notepad and then open calculator")
                _id, status, _summary = self.run_plan(plan)
                self.assertEqual(status, "failed")
                self.assertIn(fragment, plan.steps[0].error)
                self.assertEqual(plan.steps[1].status, "skipped")
        self.fakes.launch_error, self.fakes.exit_code = None, 0
        plan = self.plan("open notepad and then open calculator")
        self.assertEqual(self.run_plan(plan)[1], "succeeded", "a clean exit (launcher stub) counts as started")

    def test_browser_failure_is_not_reported_as_success(self):
        self.fakes.browser_ok = False
        plan = self.plan("open youtube and gmail")
        _id, status, _ = self.run_plan(plan)
        self.assertEqual((status, plan.steps[0].status), ("failed", "failed"))

    def test_timeout_stops_the_task(self):
        self.fakes.search_delay = 2
        plan = self.plan("open my downloads folder and search it for invoice and then open notepad")
        started = time.monotonic()
        _id, status, summary = self.run_plan(plan, timeouts={"search_files": 0.3})
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(status, "timed_out")
        self.assertEqual([s.status for s in plan.steps], ["succeeded", "timed_out", "skipped"])
        self.assertEqual(self.fakes.launched, [], "nothing after the timeout ran")
        self.assertIn("took too long", summary)

    def test_cancel_before_and_between_steps(self):
        cancel = threading.Event()
        cancel.set()
        plan = self.plan("open notepad and then open calculator")
        _id, status, _ = self.run_plan(plan, cancel=cancel)
        self.assertEqual((status, [s.status for s in plan.steps]), ("cancelled", ["cancelled", "cancelled"]))
        self.assertEqual(self.fakes.launched, [])

        cancel = threading.Event()
        original = self.fakes.launch

        def launch_then_cancel(argv):
            cancel.set()  # the user presses Stop while step 1 is running
            return original(argv)

        self.fakes.launch = launch_then_cancel
        self.services.launch = launch_then_cancel
        plan = self.plan("open notepad and then open calculator")
        _id, status, _ = self.run_plan(plan, cancel=cancel)
        self.assertEqual((status, [s.status for s in plan.steps]), ("cancelled", ["succeeded", "cancelled"]))
        self.assertEqual(self.fakes.launched, [["notepad.exe"]])

    def test_sensitive_steps_ask_and_can_be_declined(self):
        asked = []
        plan = self.plan(f"open the folder {self.fakes.outside} and open notepad")
        _id, status, _ = self.run_plan(plan, confirm=lambda step: asked.append(step.description) or False)
        self.assertEqual(status, "cancelled")
        self.assertEqual([s.status for s in plan.steps], ["declined", "cancelled"])
        self.assertEqual((self.fakes.folders, self.fakes.launched), ([], []))
        self.assertEqual(len(asked), 1, "only the sensitive step asks")
        plan = self.plan(f"open the folder {self.fakes.outside} and open notepad")
        self.assertEqual(self.run_plan(plan, confirm=lambda step: True)[1], "succeeded")

    def test_folder_changed_after_planning_fails_safely(self):
        target = self.fakes.home / "Projects" / "nova"
        plan = self.plan(f"open the folder {target} in vs code and open notepad")
        shutil.rmtree(target)
        _id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "rejected", "re-checked before any step runs")
        self.assertIn("doesn't exist", summary)
        self.assertEqual([s.status for s in plan.steps], ["skipped", "skipped"])
        self.assertEqual(self.fakes.launched, [], "not even the independent Notepad step ran")

    def test_vscode_gets_a_fixed_argument_list_and_never_a_batch_file(self):
        target = self.fakes.home / "Projects" / "nova"
        plan = self.plan(f"open the folder {target} in vs code and open notepad")
        self.run_plan(plan)
        self.assertEqual(self.fakes.launched[0], [str(self.fakes.code_exe), str(target)])
        shim = self.tmp / "code.cmd"
        shim.write_text("@echo off")
        self.services.find_vscode = lambda: str(shim)
        plan = self.plan(f"open the folder {target} in vs code and open notepad")
        _id, status, _ = self.run_plan(plan)
        self.assertEqual(status, "failed")
        self.assertIn("Code.exe", plan.steps[0].error)

    def test_each_run_executes_once_and_one_at_a_time(self):
        plan = self.plan("open notepad and then open calculator")
        run_id, status, _ = self.run_plan(plan)
        with self.assertRaises(nt.AlreadyRunning):
            self.runner.run(run_id, plan, threading.Event(), lambda s: True)
        self.assertEqual(len(self.fakes.launched), 2, "the second attempt launched nothing")

        gate = threading.Event()
        self.fakes.search_delay = 0
        slow = self.plan("open my downloads folder and search it for invoice")
        self.services.search_files = lambda *a: (gate.wait(5), ([], False))[1]
        con = assistant.connect_db()
        first, second = nt.create_run(con, slow), nt.create_run(con, slow)
        con.close()
        thread = threading.Thread(target=self.runner.run, args=(first, slow, threading.Event(), lambda s: True))
        thread.start()
        time.sleep(0.2)
        self.assertTrue(self.runner.busy)
        with self.assertRaises(nt.AlreadyRunning):
            self.runner.run(second, slow, threading.Event(), lambda s: True)
        gate.set()
        thread.join(5)

    def test_recent_duplicate_detection(self):
        plan = self.plan("open notepad and then open calculator")
        self.run_plan(plan)
        con = assistant.connect_db()
        try:
            again = self.plan("open notepad and then open calculator")
            self.assertIsNotNone(nt.recent_duplicate(con, again))
            self.assertIsNone(nt.recent_duplicate(con, self.plan("open notepad and then open paint")))
            con.execute("UPDATE task_runs SET finished_at = ?",
                        ((datetime.now().astimezone() - timedelta(minutes=10)).isoformat(),))
            con.commit()
            self.assertIsNone(nt.recent_duplicate(con, again))
        finally:
            con.close()

    def test_history_records_rejections_cancellations_and_crashes(self):
        con = assistant.connect_db()
        try:
            rejected = self.plan("task: delete everything")
            nt.create_run(con, rejected, status="rejected")
            nt.create_run(con, self.plan("open notepad and then open paint"), status="cancelled")
            crashed = nt.create_run(con, self.plan("open youtube and gmail"))
            nt.claim_run(con, crashed)
            nt.update_step(con, crashed, 0, "running")
        finally:
            con.close()
        con = assistant.connect_db()
        nt.initialize_schema(con)  # next start-up
        con.close()
        statuses = {run["status"] for run in self.history()}
        self.assertEqual(statuses, {"rejected", "cancelled", "interrupted"})
        crash = next(r for r in self.history() if r["status"] == "interrupted")
        self.assertEqual([s["status"] for s in crash["steps"]], ["interrupted", "interrupted"])
        rejected_run = next(r for r in self.history() if r["status"] == "rejected")
        self.assertIn("Not run", rejected_run["summary"])


class GuiGlueTests(TaskTest):
    @classmethod
    def setUpClass(cls):
        import nova_gui
        cls.gui = nova_gui

    def app(self):
        app = mock.Mock()
        app.events = queue.Queue()
        app.settings = {}
        app.task_runner = self.runner
        return app

    def events(self, app):
        return [app.events.get() for _ in range(app.events.qsize())]

    def test_multi_step_message_becomes_a_plan_for_review(self):
        app = self.app()
        self.gui.NovaGUI._action_worker(app, 1, "open notepad and then open calculator", False)
        (event, payload), = self.events(app)
        self.assertEqual(event, "task_plan")
        self.assertEqual([s.action for s in payload[2].steps], ["open_app", "open_app"])
        self.assertEqual(self.fakes.launched, [], "nothing runs before the plan is approved")

    def test_single_commands_keep_their_old_path(self):
        app = self.app()
        with mock.patch.object(self.gui, "run_local_action", return_value="Opened Notepad.") as action, \
                mock.patch.object(self.gui, "save_message_safe"):
            self.gui.NovaGUI._action_worker(app, 1, "open notepad", False)
        action.assert_called_once_with("open notepad")
        self.assertEqual(self.events(app)[-1][0], "action_done")

    def test_find_vscode_never_returns_the_batch_shim(self):
        shim_dir = self.tmp / "Microsoft VS Code" / "bin"
        shim_dir.mkdir(parents=True)
        (shim_dir / "code.cmd").write_text("@echo off")
        (shim_dir.parent / "Code.exe").write_text("")
        with mock.patch.object(self.gui.shutil, "which", return_value=str(shim_dir / "code.cmd")):
            self.assertEqual(Path(self.gui.find_vscode_exe()).name, "Code.exe")
        with mock.patch.object(self.gui.shutil, "which", return_value=None), \
                mock.patch.dict(self.gui.os.environ, {"LOCALAPPDATA": str(self.tmp / "none"),
                                                      "ProgramFiles": str(self.tmp / "none"),
                                                      "ProgramFiles(x86)": str(self.tmp / "none")}):
            self.assertIsNone(self.gui.find_vscode_exe())

    def test_shared_file_search(self):
        root = self.fakes.home
        (root / "Documents" / ".hidden").mkdir()
        (root / "Documents" / ".hidden" / "invoice-secret.txt").write_text("x")
        (root / "Documents" / "invoice-may.txt").write_text("x")
        matches, stopped = self.gui.search_filenames(root, "INVOICE")
        self.assertEqual(sorted(p.name for p in matches), ["invoice-2026.pdf", "invoice-may.txt"])
        self.assertFalse(stopped)
        self.assertTrue(self.gui.search_filenames(root, "invoice", should_stop=lambda: True)[1])

    def test_real_services_only_expose_fixed_operations(self):
        services = self.gui.task_services()
        self.assertEqual(set(nt.REGISTRY), {"open_website", "open_app", "open_folder", "open_folder_in_vscode",
                                            "search_files", "create_note", "schedule_reminder",
                                            # Step 25 (nova_actions.py)
                                            "switch_app", "open_file", "read_document", "create_folder",
                                            "create_file", "copy_file", "move_file", "rename_file", "recycle_file",
                                            "copy_text_to_clipboard", "save_clipboard_as_note"})
        for action in nt.REGISTRY.values():
            self.assertIn(action.permission, nt.PERMISSION_LABELS, action.name)
        for name, (_title, argv) in nt.APPS.items():
            self.assertTrue(argv is None or all(isinstance(a, str) and " " not in a for a in argv), name)
        self.assertEqual(set(services.known_folders), set(nt.KNOWN_FOLDERS))


if __name__ == "__main__":
    unittest.main()

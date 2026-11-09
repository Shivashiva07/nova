"""Tests for Step 25's computer actions (nova_actions.py): paths, permissions,
confirmations, overwrite protection, timeouts, partial failures, the action
record, and untrusted model plans.

Files are created in scratch folders next to this file (not under AppData,
which Nova never touches) and removed afterwards. Windows features that would
change the PC (Recycle Bin, other apps' windows) are replaced by fakes.

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
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_actions as na  # noqa: E402
import nova_tasks as nt  # noqa: E402

HERE = Path(__file__).resolve().parent


class ActionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="_actions_", dir=HERE)).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.allowed = self.root / "Allowed"
        self.other = self.root / "Other"
        self.protected = self.root / "Protected"
        self.known = self.root / "Documents"
        for folder in (self.allowed, self.other, self.protected, self.known, self.allowed / "Sub"):
            folder.mkdir()
        (self.allowed / "report.docx").write_bytes(b"report v1")
        (self.allowed / "notes.txt").write_text("hello", encoding="utf-8")
        (self.other / "photo.jpg").write_bytes(b"jpg")
        (self.known / "invoice.pdf").write_bytes(b"%PDF")
        (self.protected / "secret.txt").write_text("x", encoding="utf-8")
        self.opened, self.recycled, self.notes, self.switched = [], [], {}, []
        self.clip = {"text": ""}
        self.services = nt.Services(
            open_url=lambda url: True, launch=mock.Mock(), open_folder=lambda path: None,
            find_vscode=lambda: None, search_files=lambda *a: ([], False),
            save_note=self._save_note, get_note=lambda nid: (nid, *self.notes[nid]) if nid in self.notes else None,
            save_reminder=lambda *a: 1, get_reminder=lambda rid: None,
            known_folders={"documents": self.known}, home=self.root,
            approved_folders=lambda: [str(self.allowed)], protected=lambda: [str(self.protected)],
            open_file=self.opened.append, switch_window=self._switch, recycle=self._recycle,
            clipboard_get=lambda: self.clip["text"], clipboard_set=lambda text: self.clip.update(text=text),
            index_document=lambda path, stop: ("indexed", 3, None),
        )
        self.db = Path(tempfile.mkdtemp(prefix="nova_actions_db_"))
        self.addCleanup(shutil.rmtree, self.db, True)
        patcher = mock.patch.object(assistant, "DB_PATH", self.db / "nova.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        con = assistant.connect_db()
        nt.initialize_schema(con)
        con.close()
        self.runner = nt.TaskRunner(assistant.connect_db, self.services)

    def _save_note(self, title, content):
        note_id = len(self.notes) + 1
        self.notes[note_id] = (title, content)
        return note_id

    def _switch(self, app):
        self.switched.append(app)
        return {"notepad": True, "paint": False}.get(app)

    def _recycle(self, path):
        self.recycled.append(path)
        os.remove(path)

    # helpers
    def plan(self, *steps, planner="rules", picked=()):
        plan = nt.Plan("test", [nt.Step(a, dict(p)) for a, p in steps], planner, picked=set(picked))
        return nt.validate_plan(plan, self.services)

    def run_plan(self, plan, confirm=lambda step: True, timeouts=None, cancel=None):
        con = assistant.connect_db()
        run_id = nt.create_run(con, plan)
        con.close()
        status, summary = self.runner.run(run_id, plan, cancel or threading.Event(), confirm, timeouts)
        return run_id, status, summary

    def problems(self, action, params, **kw):
        plan = self.plan((action, params), **kw)
        return " ".join(plan.problems)


# ==================== paths ====================
class PathTests(ActionTest):
    def test_invalid_paths_are_rejected(self):
        cases = {
            "relative": "report.docx",
            "drive-relative": "C:report.docx",
            "network": r"\\server\share\report.docx",
            "device": r"\\?\C:\Windows\notepad.exe",
            "dot-dot": str(self.allowed / ".." / "Other" / "photo.jpg").replace("\\..\\", "\\..\\"),
            "dot": rf"{self.allowed}\.\report.docx",
            "stream": rf"{self.allowed}\report.docx:hidden",
            "wildcard": rf"{self.allowed}\*.docx",
            "reserved name": rf"{self.allowed}\CON.txt",
            "trailing dot": rf"{self.allowed}\report.docx.",
            "short name": r"C:\PROGRA~1\app.txt",
            "control character": f"{self.allowed}\\rep\x07ort.docx",
            "too long": "C:\\" + "a" * 2000,
            "empty": "",
        }
        for name, value in cases.items():
            with self.subTest(name):
                with self.assertRaises(nt.PlanError):
                    na.checked_file(value, self.services)

    def test_dot_dot_is_refused_even_when_it_lands_inside(self):
        sneaky = rf"{self.allowed}\Sub\..\report.docx"
        self.assertIn("“..”", self.problems("rename_file", {"file": sneaky, "name": "x.docx"}))

    def test_links_and_junctions_are_never_followed(self):
        import _winapi
        junction = self.allowed / "Shortcut"
        try:
            _winapi.CreateJunction(str(self.other), str(junction))
        except OSError as exc:
            self.skipTest(f"can't create a junction here: {exc}")
        self.assertTrue(na.is_link(junction))
        through = junction / "photo.jpg"
        self.assertIn("link or junction", self.problems("copy_file", {"file": str(through),
                                                                      "destination": str(self.allowed / "Sub")}))
        self.assertIn("link or junction", self.problems("copy_file", {"file": str(self.allowed / "notes.txt"),
                                                                      "destination": str(junction)}))
        self.assertIn("link or junction", self.problems("recycle_file", {"file": str(through)}))
        self.assertTrue((self.other / "photo.jpg").exists())

    def test_symbolic_links_are_never_followed(self):
        link = self.allowed / "link.txt"
        try:
            os.symlink(self.other / "photo.jpg", link)
        except OSError as exc:
            self.skipTest(f"symbolic links need Developer Mode here: {exc}")
        self.assertIn("link or junction", self.problems("rename_file", {"file": str(link), "name": "y.txt"}))

    def test_protected_locations_and_secret_files(self):
        self.assertIn("protected location", self.problems("open_file", {"file": str(self.protected / "secret.txt")}))
        appdata = self.allowed / "AppData" / "Local"
        appdata.mkdir(parents=True)
        (appdata / "Login Data").write_bytes(b"x")
        self.assertIn("AppData", self.problems("copy_file", {"file": str(appdata / "Login Data"),
                                                             "destination": str(self.allowed / "Sub")}))
        for name in ("id_rsa", "vault.kdbx", "server.pem", ".env", "Cookies"):
            (self.allowed / name).write_bytes(b"x")
            with self.subTest(name):
                self.assertIn("password, key", self.problems("copy_file", {"file": str(self.allowed / name),
                                                                           "destination": str(self.allowed / "Sub")}))
        self.assertIn("password, key", self.problems("rename_file", {"file": str(self.allowed / "notes.txt"),
                                                                     "name": "id_rsa"}))

    def test_whole_folders_are_refused(self):
        self.assertIn("is a folder", self.problems("recycle_file", {"file": str(self.allowed / "Sub")}))
        for text in ("delete everything", "delete my documents", r"delete the folder C:\Users",
                     "move my downloads to the desktop", "copy all files to D:\\Backup", r"delete C:\ "):
            with self.subTest(text):
                steps, problems = nt.plan_with_rules(text)
                self.assertTrue(problems and problems[0].startswith(nt.FORBIDDEN_PREFIX), (steps, problems))


# ==================== approvals and permissions ====================
class PermissionTests(ActionTest):
    def test_changes_outside_allowed_folders_need_permission(self):
        plan = self.plan(("copy_file", {"file": str(self.allowed / "notes.txt"), "destination": str(self.other)}))
        self.assertTrue(plan.runnable, "planning is fine: the window asks about the folder")
        self.assertEqual(plan.needs_approval(), [str(self.other)])
        _id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "rejected", "the runner never changes an unapproved folder")
        self.assertIn("isn't allowed", summary)
        self.assertFalse((self.other / "notes.txt").exists())
        plan = self.plan(("copy_file", {"file": str(self.allowed / "notes.txt"), "destination": str(self.other)}),
                         picked=[str(self.other)])                    # allowed for this task only
        self.assertEqual(plan.needs_approval(), [])
        self.assertEqual(self.run_plan(plan)[1], "succeeded")
        self.assertEqual((self.other / "notes.txt").read_text(encoding="utf-8"), "hello")

    def test_reading_is_allowed_in_known_folders_but_changing_is_not(self):
        invoice = str(self.known / "invoice.pdf")
        self.assertEqual(self.plan(("open_file", {"file": invoice})).needs_approval(), [])
        self.assertEqual(self.plan(("copy_file", {"file": invoice, "destination": str(self.allowed)}))
                         .needs_approval(), [])
        self.assertEqual(self.plan(("rename_file", {"file": invoice, "name": "x.pdf"})).needs_approval(),
                         [str(self.known)])
        self.assertEqual(self.plan(("open_file", {"file": str(self.other / "photo.jpg")})).needs_approval(),
                         [str(self.other)])

    def test_permission_revoked_before_running(self):
        plan = self.plan(("create_file", {"folder": str(self.allowed), "name": "a.txt", "content": "x"}))
        self.services.approved_folders = lambda: []          # the user removed it in the meantime
        _id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "rejected")
        self.assertFalse((self.allowed / "a.txt").exists())

    def test_handlers_recheck_right_before_changing(self):
        ctx = nt.StepContext(self.services, threading.Event())
        self.services.approved_folders = lambda: []
        with self.assertRaises(nt.ActionError):
            na._r_create_file({"folder": str(self.allowed), "name": "a.txt", "content": ""}, ctx)
        self.assertFalse((self.allowed / "a.txt").exists())

    def test_every_action_has_a_permission_and_timeout(self):
        for action in nt.REGISTRY.values():
            with self.subTest(action.name):
                self.assertIn(action.permission, nt.PERMISSION_LABELS)
                self.assertGreater(action.timeout, 0)
        dangerous = {a.name for a in nt.REGISTRY.values() if a.permission == nt.DESTRUCTIVE}
        self.assertEqual(dangerous, {"move_file", "rename_file", "recycle_file"})


# ==================== confirmations, overwrite protection ====================
class ConfirmationTests(ActionTest):
    def test_moves_renames_and_recycling_always_ask(self):
        cases = [("move_file", {"file": str(self.allowed / "notes.txt"), "destination": str(self.allowed / "Sub")}),
                 ("rename_file", {"file": str(self.allowed / "notes.txt"), "name": "renamed.txt"}),
                 ("recycle_file", {"file": str(self.allowed / "notes.txt")})]
        for action, params in cases:
            with self.subTest(action):
                plan = self.plan((action, params))
                self.assertTrue(plan.steps[0].sensitive)
                asked = []
                _id, status, _summary = self.run_plan(plan, confirm=lambda step: asked.append(step) or False)
                self.assertEqual((status, len(asked)), ("cancelled", 1))
                self.assertEqual(plan.steps[0].status, "declined")
                self.assertTrue((self.allowed / "notes.txt").exists(), "nothing changed when declined")
        self.assertEqual(self.recycled, [])

    def test_cancelling_skips_the_remaining_steps(self):
        plan = self.plan(("create_file", {"folder": str(self.allowed), "name": "first.txt", "content": "1"}),
                         ("move_file", {"file": str(self.allowed / "notes.txt"), "destination": str(self.allowed / "Sub")}),
                         ("create_folder", {"folder": str(self.allowed), "name": "Later"}))
        _id, status, summary = self.run_plan(plan, confirm=lambda step: False)
        self.assertEqual(status, "cancelled")
        self.assertEqual([s.status for s in plan.steps], ["succeeded", "declined", "cancelled"])
        self.assertTrue((self.allowed / "first.txt").exists())
        self.assertFalse((self.allowed / "Later").exists())
        self.assertIn("Task cancelled", summary)

    def test_never_overwrites_without_asking(self):
        (self.allowed / "Sub" / "report.docx").write_bytes(b"old")
        source, dest = str(self.allowed / "report.docx"), str(self.allowed / "Sub")
        self.assertIn("already exists", self.problems("copy_file", {"file": source, "destination": dest}))
        plan = self.plan(("copy_file", {"file": source, "destination": dest, "overwrite": True}))
        self.assertIn("replaces an existing file", plan.steps[0].sensitive)
        self.run_plan(plan, confirm=lambda step: False)
        self.assertEqual((self.allowed / "Sub" / "report.docx").read_bytes(), b"old")
        plan = self.plan(("copy_file", {"file": source, "destination": dest, "overwrite": True}))
        _id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded")
        self.assertEqual((self.allowed / "Sub" / "report.docx").read_bytes(), b"report v1")

    def test_a_file_appearing_after_planning_is_not_overwritten(self):
        plan = self.plan(("copy_file", {"file": str(self.allowed / "report.docx"),
                                        "destination": str(self.allowed / "Sub")}))
        (self.allowed / "Sub" / "report.docx").write_bytes(b"someone else's")
        _id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "rejected", "re-validated at run time")
        self.assertEqual((self.allowed / "Sub" / "report.docx").read_bytes(), b"someone else's")
        ctx = nt.StepContext(self.services, threading.Event())       # and inside the handler itself
        with self.assertRaises(nt.ActionError):
            na._r_copy({"file": str(self.allowed / "report.docx"), "destination": str(self.allowed / "Sub"),
                        "overwrite": False}, ctx)
        self.assertEqual((self.allowed / "Sub" / "report.docx").read_bytes(), b"someone else's")
        self.assertEqual([p.name for p in (self.allowed / "Sub").iterdir()], ["report.docx"], "no temp file left")

    def test_hard_linked_files_are_not_replaced(self):
        target = self.allowed / "Sub" / "report.docx"
        outside = self.other / "elsewhere.docx"
        outside.write_bytes(b"keep me")
        os.link(outside, target)
        plan = self.plan(("copy_file", {"file": str(self.allowed / "report.docx"),
                                        "destination": str(self.allowed / "Sub"), "overwrite": True}))
        _id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "failed")
        self.assertIn("hard link", plan.steps[0].error)
        self.assertEqual(outside.read_bytes(), b"keep me")

    def test_create_and_rename_never_overwrite_or_make_programs(self):
        self.assertIn("already exists", self.problems("create_file", {"folder": str(self.allowed),
                                                                       "name": "notes.txt", "content": ""}))
        self.assertIn("run code", self.problems("create_file", {"folder": str(self.allowed), "name": "run.bat",
                                                                "content": "del *"}))
        self.assertIn("text only", self.problems("create_file", {"folder": str(self.allowed), "name": "x.docx"}))
        self.assertIn("run code", self.problems("rename_file", {"file": str(self.allowed / "notes.txt"),
                                                                "name": "notes.exe"}))
        self.assertIn("already exists", self.problems("rename_file", {"file": str(self.allowed / "notes.txt"),
                                                                      "name": "report.docx"}))
        plan = self.plan(("rename_file", {"file": str(self.allowed / "report.docx"), "name": "final"}))
        self.assertEqual(plan.steps[0].params["name"], "final.docx", "the extension is kept")


# ==================== running, timeouts, partial failures ====================
class RunTests(ActionTest):
    def test_file_operations_succeed_and_are_verified(self):
        plan = self.plan(
            ("create_folder", {"folder": str(self.allowed), "name": "Archive"}),
            ("create_file", {"folder": str(self.allowed), "name": "todo", "content": "buy milk"}),
            ("copy_file", {"file": str(self.allowed / "report.docx"), "destination": str(self.allowed / "Sub")}),
            ("rename_file", {"file": str(self.allowed / "notes.txt"), "name": "hello.txt"}),
            ("recycle_file", {"file": str(self.allowed / "hello.txt")}) if False else
            ("open_file", {"file": str(self.allowed / "report.docx")}),
        )
        self.assertTrue(plan.runnable, plan.problems)
        _id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded", summary)
        self.assertTrue((self.allowed / "Archive").is_dir())
        self.assertEqual((self.allowed / "todo.txt").read_text(encoding="utf-8"), "buy milk")
        self.assertEqual((self.allowed / "Sub" / "report.docx").read_bytes(), b"report v1")
        self.assertTrue((self.allowed / "report.docx").exists(), "a copy keeps the original")
        self.assertTrue((self.allowed / "hello.txt").exists())
        self.assertEqual(self.opened, [str(self.allowed / "report.docx")])

    def test_move_and_recycle(self):
        plan = self.plan(("move_file", {"file": str(self.allowed / "notes.txt"),
                                        "destination": str(self.allowed / "Sub")}),
                         ("recycle_file", {"file": str(self.allowed / "report.docx")}))
        _id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded", summary)
        self.assertTrue((self.allowed / "Sub" / "notes.txt").exists())
        self.assertFalse((self.allowed / "notes.txt").exists())
        self.assertEqual(self.recycled, [str(self.allowed / "report.docx")])
        self.assertIn("Recycle Bin", plan.steps[1].result)

    def test_a_failed_step_stops_the_task(self):
        plan = self.plan(("create_file", {"folder": str(self.allowed), "name": "one.txt", "content": "1"}),
                         ("copy_file", {"file": str(self.allowed / "report.docx"),
                                        "destination": str(self.allowed / "Sub")}),
                         ("create_folder", {"folder": str(self.allowed), "name": "Never"}))
        recycled_meanwhile = []

        def flaky_copy(*args, **kwargs):
            recycled_meanwhile.append(True)
            raise nt.ActionError("The disk is full.")

        with mock.patch.object(na, "_copy_contents", flaky_copy):
            _id, status, summary = self.run_plan(plan)
        self.assertEqual(status, "failed")
        self.assertEqual([s.status for s in plan.steps], ["succeeded", "failed", "skipped"])
        self.assertTrue((self.allowed / "one.txt").exists())
        self.assertFalse((self.allowed / "Never").exists())
        self.assertIn("1 of 3 succeeded", summary)
        self.assertIn("The disk is full", summary)

    def test_a_slow_copy_times_out_and_leaves_nothing_behind(self):
        big = self.allowed / "big.bin"
        with open(big, "wb") as handle:
            handle.write(os.urandom(1024 * 1024) * 24)          # 24 MB
        plan = self.plan(("copy_file", {"file": str(big), "destination": str(self.allowed / "Sub")}))
        real_read = open

        def slow_open(path, mode="r", *args, **kwargs):     # a slow disk: each chunk takes a while
            handle = real_read(path, mode, *args, **kwargs)
            if "r" in mode and "b" in mode and str(path) == str(big):
                read = handle.read
                handle_read = lambda size=-1: (time.sleep(0.02), read(size))[1]
                return mock.Mock(wraps=handle, read=handle_read, __enter__=lambda s: s,
                                 __exit__=lambda s, *a: handle.close())
            return handle

        with mock.patch.object(na, "COPY_CHUNK", 256 * 1024), mock.patch("builtins.open", slow_open):
            started = time.monotonic()
            _id, status, summary = self.run_plan(plan, timeouts={"copy_file": 0.3})
        self.assertEqual(status, "timed_out")
        self.assertLess(time.monotonic() - started, 0.3 + nt.STOP_GRACE + 1)
        self.assertIn("nothing was changed", plan.steps[0].error.lower())
        self.assertEqual(list((self.allowed / "Sub").iterdir()), [], "no partial or temporary file")

    def test_stop_between_chunks(self):
        stop = threading.Event()
        stop.set()
        ctx = nt.StepContext(self.services, stop)
        with self.assertRaises(nt.ActionError) as caught:
            na._copy_contents(self.allowed / "report.docx", self.allowed / "Sub", "report.docx", ctx, False)
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(list((self.allowed / "Sub").iterdir()), [])

    def test_windows_permission_errors_are_reported_not_bypassed(self):
        plan = self.plan(("create_file", {"folder": str(self.allowed), "name": "a.txt", "content": "x"}))
        with mock.patch("builtins.open", side_effect=PermissionError(13, "Access is denied")):
            _id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "failed")
        self.assertIn("didn't allow", plan.steps[0].error)

    def test_switching_apps(self):
        for app, status, fragment in (("notepad", "succeeded", "Switched to Notepad"),
                                      ("vscode", "failed", "isn't open"),
                                      ("paint", "failed", "didn't let Nova")):
            with self.subTest(app):
                plan = self.plan(("switch_app", {"app": app}))
                _id, got, _summary = self.run_plan(plan)
                self.assertEqual(got, status)
                self.assertIn(fragment, plan.steps[0].result or plan.steps[0].error)
        self.assertTrue(nt.plan_request("switch to notepad", self.services).auto_run)
        self.assertFalse(nt.plan_request(r"open " + str(self.allowed / "report.docx"), self.services).auto_run)

    def test_reading_a_document_records_no_content(self):
        plan = self.plan(("read_document", {"file": str(self.allowed / "report.docx")}))
        _id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded")
        self.assertIn("3 passages", plan.steps[0].result)
        self.assertIn("doesn't", " ".join(self.plan(("read_document", {"file": str(self.other / "photo.jpg")}))
                                          .problems) + "doesn't")
        self.assertIn("can read", self.problems("read_document", {"file": str(self.other / "photo.jpg")}))

    def test_opening_files_never_runs_programs(self):
        for name in ("setup.exe", "run.bat", "shortcut.lnk", "page.html", "script.ps1", "noext"):
            (self.allowed / name).write_bytes(b"x")
            with self.subTest(name):
                self.assertIn("Nova only opens", self.problems("open_file", {"file": str(self.allowed / name)}))
        self.assertEqual(self.opened, [])


# ==================== clipboard and the action record ====================
class ClipboardAndRecordTests(ActionTest):
    def test_clipboard_actions_ask_first(self):
        plan = self.plan(("copy_text_to_clipboard", {"text": "Meeting at 4 pm"}))
        self.assertIn("clipboard", plan.steps[0].sensitive)
        self.run_plan(plan, confirm=lambda step: False)
        self.assertEqual(self.clip["text"], "", "declined: clipboard untouched")
        plan = self.plan(("copy_text_to_clipboard", {"text": "Meeting at 4 pm"}))
        self.assertEqual(self.run_plan(plan)[1], "succeeded")
        self.assertEqual(self.clip["text"], "Meeting at 4 pm")

    def test_clipboard_secrets_are_never_read_into_notes(self):
        for secret in ("password: hunter2", "ghp_abcdefghijklmnopqrstuvwxyz0123", "Tr0ub4dor&3xyz",
                       "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIz",
                       "-----BEGIN RSA PRIVATE KEY-----\nabc"):
            with self.subTest(secret[:12]):
                self.clip["text"] = secret
                plan = self.plan(("save_clipboard_as_note", {"title": "Clip"}))
                _id, status, _summary = self.run_plan(plan)
                self.assertEqual(status, "failed")
                self.assertIn("password, key or token", plan.steps[0].error)
        self.assertEqual(self.notes, {})
        self.clip["text"] = "Buy milk and eggs on the way home"
        plan = self.plan(("save_clipboard_as_note", {"title": "Clip"}))
        self.assertEqual(self.run_plan(plan)[1], "succeeded")
        self.assertEqual(self.notes[1], ("Clip", "Buy milk and eggs on the way home"))

    def test_ordinary_text_isnt_mistaken_for_secrets(self):
        for text in ("Buy milk", "https://www.example.com/page?id=4", "asha@example.com", r"C:\Users\me\file.txt",
                     "The meeting moved to Friday at 4 pm.", "ThisIsALongWordOnly"):
            with self.subTest(text):
                self.assertFalse(na.looks_secret(text))

    def test_history_never_stores_contents(self):
        self.clip["text"] = "Private clipboard text about my salary"
        plan = self.plan(("create_file", {"folder": str(self.allowed), "name": "diary", "content": "Dear diary, today"}),
                         ("copy_text_to_clipboard", {"text": "my secret plans"}),
                         ("save_clipboard_as_note", {"title": "Clip"}))
        run_id, status, _summary = self.run_plan(plan)
        self.assertEqual(status, "succeeded")
        con = assistant.connect_db()
        stored = json.dumps([tuple(r) for r in con.execute("SELECT * FROM task_steps WHERE run_id = ?", (run_id,))])
        stored += json.dumps([tuple(r) for r in con.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,))])
        con.close()
        for private in ("Dear diary", "my secret plans", "salary"):
            self.assertNotIn(private, stored)
        self.assertIn("characters, not stored", stored)
        self.assertIn(str(self.allowed / "diary.txt").replace("\\", "\\\\"), stored, "paths are recorded")


# ==================== untrusted model output ====================
class ModelPlanTests(ActionTest):
    def model(self, payload):
        return mock.Mock(return_value={"message": {"content": json.dumps(payload)}})

    def model_plan(self, payload):
        return nt.plan_request("task: tidy up my files", self.services, chat=self.model(payload), model="m")

    def test_model_plans_cannot_bypass_the_registry(self):
        notes = str(self.allowed / "notes.txt")
        attempts = [
            {"steps": [{"action": "delete_folder", "params": {"folder": str(self.allowed)}}]},
            {"steps": [{"action": "copy_file", "params": {"file": notes, "destination": str(self.allowed / "Sub"),
                                                          "approved": True}}]},
            {"steps": [{"action": "copy_file", "params": {"file": notes, "destination": str(self.protected)}}]},
            {"steps": [{"action": "copy_file", "params": {"file": notes,
                                                          "destination": str(self.allowed) + r"\Sub\..\.."}}]},
            {"steps": [{"action": "recycle_file", "params": {"file": str(self.allowed)}}]},
            {"steps": [{"action": "open_file", "params": {"file": r"C:\Windows\System32\cmd.exe"}}]},
            {"steps": [{"action": "rename_file", "params": {"file": notes, "name": r"..\..\evil.txt"}}]},
            {"steps": [{"action": "create_file", "params": {"folder": str(self.allowed), "name": "x.ps1",
                                                            "content": "Remove-Item C:\\ -Recurse"}}]},
            {"steps": [{"action": "move_file", "params": {"file": notes, "destination": str(self.allowed / "Sub")},
                        "confirmed": True}]},
        ]
        for payload in attempts:
            with self.subTest(payload=str(payload)[:80]):
                plan = self.model_plan(payload)
                self.assertFalse(plan.runnable, plan.problems)
                _id, status, _summary = self.run_plan(plan)
                self.assertEqual(status, "rejected")
        self.assertTrue((self.allowed / "notes.txt").exists())
        self.assertEqual(sorted(p.name for p in self.allowed.iterdir()), ["Sub", "notes.txt", "report.docx"])

    def test_model_cannot_skip_confirmation_or_approval(self):
        (self.allowed / "Sub" / "notes.txt").write_text("old", encoding="utf-8")
        plan = self.model_plan({"steps": [{"action": "copy_file", "params": {
            "file": str(self.allowed / "notes.txt"), "destination": str(self.allowed / "Sub"), "overwrite": "yes"}}]})
        self.assertTrue(plan.runnable, plan.problems)
        self.assertIn("replaces an existing file", plan.steps[0].sensitive, "confirmation decided in code")
        plan = self.model_plan({"steps": [{"action": "create_file", "params": {
            "folder": str(self.other), "name": "a.txt", "content": "x"}}]})
        self.assertIn("suggested by the AI model", plan.steps[0].sensitive)
        self.assertEqual(plan.needs_approval(), [str(self.other)], "the model can't approve a folder")
        self.assertEqual(self.run_plan(plan)[1], "rejected")
        self.assertFalse((self.other / "a.txt").exists())


# ==================== the deterministic planner ====================
class ParserTests(ActionTest):
    def test_phrases(self):
        report = str(self.allowed / "report.docx")
        cases = {
            "switch to notepad": [("switch_app", {"app": "notepad"})],
            "bring up my browser": [("switch_app", {"app": "browser"})],
            f'copy "{report}" to a folder I choose': [("copy_file", {"file": report, "destination": "ask",
                                                                      "overwrite": False})],
            f"move {report} to {self.allowed / 'Sub'} replacing the existing file":
                [("move_file", {"file": report, "destination": str(self.allowed / "Sub"), "overwrite": True})],
            f"rename {report} to final": [("rename_file", {"file": report, "name": "final"})],
            f"delete {report}": [("recycle_file", {"file": report})],
            "move a file I choose to the recycle bin": [("recycle_file", {"file": "ask"})],
            f"open {report}": [("open_file", {"file": report})],
            "open a file I choose": [("open_file", {"file": "ask"})],
            "read a document I choose": [("read_document", {"file": "ask"})],
            f"add {report} to my documents": [("read_document", {"file": report})],
            "create a file called todo in a folder I choose saying buy milk":
                [("create_file", {"folder": "ask", "name": "todo", "content": "buy milk"})],
            f"create a folder called Archive in {self.allowed}": [("create_folder", {"folder": str(self.allowed),
                                                                                     "name": "Archive"})],
            "copy “Meeting at 4” to the clipboard": [("copy_text_to_clipboard", {"text": "Meeting at 4"})],
            "save my clipboard as a note called Ideas": [("save_clipboard_as_note", {"title": "Ideas"})],
        }
        for text, expected in cases.items():
            with self.subTest(text):
                steps, problems = nt.plan_with_rules(text)
                self.assertEqual(problems, [])
                self.assertEqual([(s.action, s.params) for s in steps], expected)

    def test_paths_with_and_in_quotes_and_follow_ups(self):
        weird = self.allowed / "Tax and Bills"
        weird.mkdir()
        steps, problems = nt.plan_with_rules(f'copy "{self.allowed / "report.docx"}" to "{weird}" and rename it '
                                             "to 2026.docx")
        self.assertEqual(problems, [])
        self.assertEqual(steps[1].params, {"file": str(weird / "report.docx"), "name": "2026.docx"})

    def test_single_file_commands_are_reviewed_tasks(self):
        plan = nt.plan_request(f"delete {self.allowed / 'notes.txt'}", self.services)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.steps[0].action, "recycle_file")
        self.assertFalse(plan.auto_run)
        self.assertIsNone(nt.plan_request("open notepad", self.services), "Step 20 single commands are unchanged")

    def test_the_old_refusals_still_hold(self):
        for text in ("run powershell Get-Process", "edit the registry", "type my password", "install python",
                     r"open C:\Windows\System32\cmd.exe", f"copy {self.allowed}\\setup.exe to D:\\x"):
            with self.subTest(text):
                problems = nt.plan_with_rules(text)[1]
                self.assertTrue(problems and problems[0].startswith(nt.FORBIDDEN_PREFIX), problems)


# ==================== the real Windows helpers (read-only or restored) ====================
class WindowsHelperTests(unittest.TestCase):
    def test_known_folders(self):
        for name in nt.KNOWN_FOLDERS:
            path = na.known_folder(name)
            self.assertTrue(path.is_absolute(), name)

    def test_clipboard_round_trip_restores_the_clipboard(self):
        try:
            original = na.clipboard_get()
        except OSError as exc:
            self.skipTest(f"clipboard unavailable: {exc}")
        try:
            na.clipboard_set("Nova clipboard test ✓")
            self.assertEqual(na.clipboard_get(), "Nova clipboard test ✓")
        finally:
            na.clipboard_set(original)

    def test_listing_windows_reads_no_content(self):
        windows = na.app_windows("explorer")
        self.assertIsInstance(windows, list)

    def test_default_protected_locations(self):
        roots = [r.lower() for r in na.default_protected(HERE.parent)]
        self.assertIn(str(HERE.parent).lower(), roots, "Nova's own folder")
        self.assertTrue(any(r.endswith("appdata") for r in roots))


if __name__ == "__main__":
    unittest.main()

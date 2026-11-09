"""The Edit / New reminder window: Save is always reachable, and edits are saved.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nova_reminders  # noqa: E402
from test_agent import AgentTest  # noqa: E402  (scratch database and folders)


class ReminderEditorTests(AgentTest):
    def setUp(self):
        super().setUp()
        import customtkinter as ctk
        import nova_gui
        self.gui = nova_gui
        for p in (mock.patch.object(nova_gui, "SETTINGS_PATH", self.db / "settings.json"),
                  mock.patch.object(nova_gui, "BACKUP_DIR", self.db / "backups"),
                  mock.patch.object(nova_gui, "load_tray_dependencies", return_value=None),
                  mock.patch.object(nova_gui, "model_context_max", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        try:
            self.root = ctk.CTk()
        except Exception as exc:
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()
        self.app = nova_gui.NovaGUI(self.root)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.app.exit_application(force=True)
            self.root.update()
        except Exception:
            pass

    def _walk(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk(child)

    def widgets(self, win, kind):
        return [w for w in self._walk(win) if w.__class__.__name__ == kind]

    def settle(self, win):
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.02)

    def save_button(self, win):
        return next(b for b in self.widgets(win, "CTkButton") if b.cget("text") == "Save")

    def assert_save_visible(self, win, repeat):
        save = self.save_button(win)
        self.assertTrue(save.winfo_ismapped(), f"Save hidden with {repeat}")
        bottom = save.winfo_rooty() + save.winfo_height()
        self.assertLessEqual(bottom, win.winfo_rooty() + win.winfo_height(), f"Save cut off with {repeat}")
        self.assertGreater(save.winfo_height(), 10, f"Save squeezed with {repeat}")

    def test_save_is_visible_for_every_repeat_and_edits_are_saved(self):
        schedule = nova_reminders.schedule_from_fields(
            (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"), "09:00", "daily")
        saved = self.gui.with_db(nova_reminders.create, "take creatine", schedule)
        reminder = saved.reminder
        win = self.app._reminder_editor(self.root, reminder)
        self.settle(win)
        segmented = self.widgets(win, "CTkSegmentedButton")[0]
        for repeat in ("Daily", "Once", "Weekly", "Monthly", "Daily"):
            segmented.set(repeat)
            segmented._command(repeat)
            self.settle(win)
            self.assert_save_visible(win, repeat)
        title = self.widgets(win, "CTkEntry")[0]
        title.delete(0, "end")
        title.insert(0, "take creatine with water")
        times = [e for e in self.widgets(win, "CTkEntry") if e.get() == "09:00"]
        times[0].delete(0, "end")
        times[0].insert(0, "08:30")
        self.save_button(win).invoke()
        self.root.update()
        self.assertFalse(win.winfo_exists(), "the window closes after saving")
        updated = self.gui.with_db(nova_reminders.get, reminder.id)
        self.assertEqual(updated.title, "take creatine with water")
        self.assertEqual((updated.rule, updated.anchor), ("daily", "08:30"))

    def test_enter_saves_a_new_reminder(self):
        saved = []
        win = self.app._reminder_editor(self.root, on_saved=lambda: saved.append(True))
        self.settle(win)
        self.assert_save_visible(win, "Once")
        self.widgets(win, "CTkEntry")[0].insert(0, "stretch")
        win.event_generate("<Return>")
        self.root.update()
        self.assertEqual(saved, [True])
        titles = [r.title for r in self.gui.with_db(nova_reminders.active)]
        self.assertIn("stretch", titles)


if __name__ == "__main__":
    unittest.main()

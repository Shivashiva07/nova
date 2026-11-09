"""Tests for performance work: bounded history, cancellation, single recordings
and replies, cancellable transcription, isolated document parsing, lazy
imports, model warm-up and performance diagnostics.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import queue
import shutil
import subprocess
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
import nova_documents as nd  # noqa: E402
import nova_perf  # noqa: E402
import nova_system  # noqa: E402


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_perf_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(assistant, "DB_PATH", self.tmp / "nova.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        assistant.initialize_db()
        import nova_gui
        self.gui = nova_gui


class HistoryWindowTests(TempDb):
    def setUp(self):
        super().setUp()
        self.session = assistant.create_session("Long chat")
        con = assistant.connect_db()
        with con:
            con.executemany("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, 'x')",
                            [(self.session, "user" if i % 2 == 0 else "assistant", f"message {i}") for i in range(500)])
        self.ids = [r[0] for r in con.execute("SELECT id FROM messages ORDER BY id")]
        con.close()

    def test_latest_page_only(self):
        rows, earlier, later = self.gui.fetch_message_window(self.session, 60)
        self.assertEqual([r[2] for r in rows][0], "message 440")
        self.assertEqual([r[2] for r in rows][-1], "message 499")
        self.assertEqual((len(rows), earlier, later), (60, 440, 0))

    def test_window_around_a_search_result(self):
        target = self.ids[100]
        rows, earlier, later = self.gui.fetch_message_window(self.session, 60, anchor_id=target, context=5)
        self.assertEqual(rows[0][2], "message 95", "a little context before the result")
        self.assertIn(target, [r[0] for r in rows])
        self.assertEqual((earlier, later), (95, 500 - 95 - 60))

    def test_helpers(self):
        self.assertEqual(self.gui.count_messages(self.session), 500)
        self.assertTrue(self.gui.has_messages(self.session))
        self.assertFalse(self.gui.has_messages(assistant.create_session()))
        recent = self.gui.recent_messages(self.session, 12)
        self.assertEqual([c for _r, c in recent], [f"message {i}" for i in range(488, 500)])
        self.assertEqual(self.gui.message_id_before(self.session, self.ids[300], 120), self.ids[180])
        self.assertEqual(self.gui.message_id_before(self.session, self.ids[3], 120), self.ids[0])


class ReplyTests(TempDb):
    def app(self):
        app = mock.Mock()
        app.events = queue.Queue()
        app._cancel_reply = threading.Event()
        return app

    def test_stop_ends_the_stream_and_keeps_the_partial_answer(self):
        app = self.app()
        session = assistant.create_session()
        consumed, closed = [], []

        def stream():
            try:
                for i in range(1000):
                    consumed.append(i)
                    if i == 5:
                        app._cancel_reply.set()      # the user pressed Stop
                    yield {"message": {"content": f"w{i} "}, "done": False}
            finally:
                closed.append(True)                  # Ollama's connection is closed

        with mock.patch.object(self.gui.ollama, "chat", return_value=stream()):
            self.gui.NovaGUI._generation_worker(app, session, "tell me a story", False, "gemma3:4b")
        self.assertTrue(self._wait(lambda: closed == [True]), "the connection to Ollama is closed")
        self.assertLess(len(consumed), 10, "no further tokens are read after Stop")
        answer = self.gui.fetch_messages(session)[-1][1]
        self.assertTrue(answer.endswith("_(Stopped.)_"))
        self.assertTrue("w0 w1 w2 w3 w4".startswith(answer.replace("_(Stopped.)_", "").strip()),
                        "only words from before Stop are kept")
        events = [app.events.get_nowait()[0] for _ in range(app.events.qsize())]
        self.assertEqual(events[-1], "generation_done")

    @staticmethod
    def _wait(condition, seconds=3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return condition()

    def test_stop_while_the_model_is_still_loading(self):
        app = self.app()
        session = assistant.create_session()
        release, closed = threading.Event(), []

        def loading_stream():
            try:
                release.wait(20)          # Ollama sends nothing while it loads the model
                yield {"message": {"content": "late"}, "done": False}
                yield {"message": {"content": " words"}, "done": True}
            finally:
                closed.append(True)

        threading.Timer(0.3, app._cancel_reply.set).start()
        started = time.monotonic()
        with mock.patch.object(self.gui.ollama, "chat", return_value=loading_stream()):
            self.gui.NovaGUI._generation_worker(app, session, "hello", False, "gemma3:4b")
        self.assertLess(time.monotonic() - started, 1.5, "Stop works before the first word arrives")
        self.assertEqual(self.gui.fetch_messages(session)[-1][1], "_(Stopped.)_")
        release.set()                     # the model finishes loading later
        self.assertTrue(self._wait(lambda: closed == [True]), "the abandoned request is then closed")

    def test_stream_errors_still_reach_the_user(self):
        app = self.app()

        def broken():
            yield {"message": {"content": "Hi"}, "done": False}
            raise ConnectionError("connection reset")

        with mock.patch.object(self.gui.ollama, "chat", return_value=broken()):
            self.gui.NovaGUI._generation_worker(app, assistant.create_session(), "hello", False, "gemma3:4b")
        events = [app.events.get_nowait() for _ in range(app.events.qsize())]
        self.assertEqual(events[-1][0], "generation_error")
        self.assertIn("couldn't reach Ollama", events[-1][1])

    def test_reply_timings_are_recorded(self):
        nova_perf.clear()
        app = self.app()
        chunks = [{"message": {"content": "Hi"}, "done": False},
                  {"message": {"content": ""}, "done": True, "eval_count": 40, "eval_duration": 2_000_000_000,
                   "load_duration": 4_900_000_000}]
        with mock.patch.object(self.gui.ollama, "chat", return_value=iter(chunks)):
            self.gui.NovaGUI._generation_worker(app, assistant.create_session(), "hi", False, "gemma3:4b")
        names = {row[0]: row for row in nova_perf.summary()}
        self.assertAlmostEqual(names["reply: generation speed"][1], 20.0)
        self.assertAlmostEqual(names["Ollama: loading the model into memory"][1], 4900.0)
        self.assertIn("reply: time to first word", names)

    def test_only_one_reply_at_a_time(self):
        app = mock.Mock(busy=True)
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI.begin_generation(app, "hello")
        thread.assert_not_called()

    def test_warm_up_is_throttled(self):
        app = mock.Mock(busy=False, _warming=False, _exiting=False, _model_used_at=-1e9)
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI._maybe_warm_model(app)
            self.gui.NovaGUI._maybe_warm_model(app)   # straight away again: skipped
        self.assertEqual(thread.call_count, 1)
        app.busy, app._model_used_at = True, -1e9
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI._maybe_warm_model(app)
        thread.assert_not_called()


class VoiceConcurrencyTests(unittest.TestCase):
    def test_second_recording_is_refused(self):
        assistant.MIC_LOCK.acquire()
        try:
            with self.assertRaises(assistant.VoiceInputError) as caught:
                assistant.record_until_silence(lambda _s: None)
            self.assertIn("already recording", str(caught.exception))
        finally:
            assistant.MIC_LOCK.release()

    def slow(self):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    def test_transcription_can_be_stopped_and_times_out(self):
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(assistant.VoiceInputError) as caught:
            assistant._run_whisper(self.slow(), cwd=None, env=None, stop_event=stop)
        self.assertIn("stopped", str(caught.exception))
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(assistant.VoiceInputError) as caught:
            assistant._run_whisper(self.slow(), cwd=None, env=None, timeout=0.5)
        self.assertIn("longer than", str(caught.exception))

    def test_whisper_uses_the_cores(self):
        self.assertGreaterEqual(assistant.WHISPER_THREADS, 2)
        self.assertLessEqual(assistant.WHISPER_THREADS, 8)


class ExtractionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_extract_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from doc_samples import make_sample_folder
        self.files = make_sample_folder(self.tmp / "docs")
        self.worker = nd.ExtractionWorker()
        self.addCleanup(self.worker.close)

    def test_results_match_in_process_extraction(self):
        for key in ("pdf", "docx", "xlsx"):
            with self.subTest(key):
                chunks, note = self.worker.run(self.files[key])
                local, _ = nd.build_chunks(nd.extract(self.files[key])[0])
                self.assertEqual([(c.content, c.location) for c in chunks], [(c.content, c.location) for c in local])
        with self.assertRaises(nd.NeedsOCR):
            self.worker.run(self.files["scan"])
        self.assertIsNotNone(self.worker._process, "one process serves the whole job")

    def test_timeout_stop_and_crash(self):
        slow = nd.ExtractionWorker(timeout=0.01)
        self.addCleanup(slow.close)
        with self.assertRaises(nd.DocumentError) as caught:
            slow.run(self.files["pdf"])
        self.assertIn("longer than", str(caught.exception))
        with self.assertRaises(nd.DocumentError) as caught:
            self.worker.run(self.files["pdf"], should_stop=lambda: True)
        self.assertIn("stopped", str(caught.exception))
        self.worker.run(self.files["docx"])
        self.worker._process.kill()               # the reader process dies
        self.worker._process.wait()
        chunks, _ = self.worker.run(self.files["docx"])   # a new one is started
        self.assertTrue(chunks)

    def test_close_leaves_no_process(self):
        self.worker.run(self.files["docx"])
        process = self.worker._process
        self.worker.close()
        self.assertIsNotNone(process.poll())

    def test_indexer_uses_the_worker_and_stops_promptly(self):
        with mock.patch.object(assistant, "DB_PATH", self.tmp / "index.db"):
            assistant.initialize_db()
            con = assistant.connect_db()
            nd.initialize_schema(con)
            con.close()
            indexer = nd.DocumentIndexer(assistant.connect_db)
            indexer.add_paths([self.tmp / "docs"])
            self.assertTrue(indexer.wait_idle(60))
            con = assistant.connect_db()
            statuses = dict(con.execute("SELECT name, status FROM doc_files").fetchall())
            con.close()
            self.assertEqual(statuses["handbook.pdf"], "indexed")
            self.assertEqual(statuses["scanned receipt.pdf"], "needs_ocr")
            self.assertIsNone(indexer._worker._process, "the reader process ends with the job")
            started = time.monotonic()
            indexer.stop()
            self.assertTrue(indexer.join(2))
            self.assertLess(time.monotonic() - started, 1.0, "an idle indexer stops at once (no polling)")


class LazyAndCacheTests(unittest.TestCase):
    def test_lazy_module(self):
        loads = []
        lazy = nova_system.LazyModule("json", on_load=loads.append)
        self.assertFalse(lazy.loaded())
        self.assertEqual(lazy.dumps([1]), "[1]")
        lazy.loads("[]")
        self.assertEqual(len(loads), 1, "set up once")
        self.assertTrue(lazy.available())
        self.assertFalse(nova_system.LazyModule("no_such_module_nova").available())
        results = []
        threaded = nova_system.LazyModule("decimal")
        workers = [threading.Thread(target=lambda: results.append(threaded.Decimal("1"))) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(len(results), 8)

    def test_vosk_model_is_loaded_once(self):
        import nova_gui
        fake = mock.Mock(side_effect=lambda path: object())
        with mock.patch.dict(sys.modules, {"vosk": mock.Mock(Model=fake)}), \
                mock.patch.dict(nova_gui._VOSK_MODELS, clear=True):
            first = nova_gui.load_vosk_model("C:/models/vosk")
            second = nova_gui.load_vosk_model("C:/models/vosk")
        self.assertIs(first, second)
        fake.assert_called_once()

    def test_heavy_packages_are_not_imported_at_startup(self):
        code = ("import sys, nova_gui; print(','.join(m for m in ('ollama', 'numpy', 'scipy', 'pypdf') "
                "if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "", "")


class WindowTests(TempDb):
    """Builds the real window on temporary data (skipped where Tk can't open one)."""

    def setUp(self):
        super().setUp()
        import customtkinter as ctk
        for p in (mock.patch.object(self.gui, "SETTINGS_PATH", self.tmp / "settings.json"),
                  mock.patch.object(self.gui, "BACKUP_DIR", self.tmp / "backups"),
                  mock.patch.object(self.gui, "load_tray_dependencies", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.gui.initialize_productivity_tables()
        self.gui.initialize_document_tables()
        self.gui.initialize_task_tables()
        con = assistant.connect_db()
        with con:
            for s in range(130):
                con.execute("INSERT INTO sessions (title, created_at, updated_at) VALUES (?, '2026-01-01T10:00:00', ?)",
                            (f"Chat {s}", f"2026-01-01T10:{s // 60:02d}:{s % 60:02d}"))
            newest = con.execute("SELECT MAX(id) FROM sessions").fetchone()[0]
            con.executemany("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, 'x')",
                            [(newest, "user" if i % 2 == 0 else "assistant", f"line {i}") for i in range(300)])
        con.close()
        try:
            self.root = ctk.CTk()
        except Exception as exc:
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()
        self.app = self.gui.NovaGUI(self.root)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.app.exit_application(force=True)
            self.root.update()
        except Exception:
            pass

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.02)

    def test_paging_and_every_settings_tab(self):
        self.pump(0.3)
        rows = self.app.session_list
        self.assertEqual(len(rows.rows), 60)
        self.assertEqual(rows.more.cget("text"), "Show more chats (70)")
        rows.more.invoke()
        self.assertEqual(len(rows.rows), 120)
        marks = [m for m in self.app.chat.text.mark_names() if m.startswith("msg_")]
        self.assertLessEqual(len(marks), self.gui.CHAT_PAGE, "a long chat opens at its latest messages")
        self.assertTrue(any("Show earlier messages" in b.cget("text") for b in self.app.chat._actions))

        import customtkinter as ctk
        self.app.open_startup_settings()
        win = self.app._settings_window
        tabs = next(w for w in self._walk(win) if isinstance(w, ctk.CTkTabview))
        for name in ("Model", "Voice & microphone", "Backup & restore", "Diagnostics", "General"):
            tabs.set(name)
            win.build_current_tab()     # what clicking the tab does
            self.pump(0.2)
        texts = [w.cget("text") for w in self._walk(tabs.tab("Diagnostics")) if isinstance(w, ctk.CTkSwitch)]
        self.assertIn("Also write performance timings to nova.log", texts)
        self.pump(4)                    # the checks run in the background
        labels = [w.cget("text") for w in self._walk(tabs.tab("Diagnostics")) if isinstance(w, ctk.CTkLabel)]
        self.assertIn("Performance", labels)
        win.destroy()

    def _walk(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk(child)


class PerfDiagnosticsTests(unittest.TestCase):
    def test_summary_and_diagnostics_rows(self):
        nova_perf.clear()
        for value in (100, 300, 200):
            nova_perf.record("transcription (whisper.cpp)", value)
        nova_perf.record("reply: generation speed", 19.5, "tokens/s")
        rows = {r[0]: r for r in nova_perf.summary()}
        self.assertEqual(rows["transcription (whisper.cpp)"][1:4], (200.0, 200.0, 3))
        import nova_diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            checks = nova_diagnostics.run_checks(Path(tmp) / "none.db", "m", tmp, Path(tmp) / "nova.log",
                                                 performance=nova_perf.summary())
        perf = {c.name: c.detail for c in checks if c.area == "Performance"}
        self.assertIn("MB", perf["Nova's memory use"])
        self.assertIn("median 200 ms over 3", perf["Transcription (whisper.cpp)"])
        self.assertIn("19.5 tokens/s", perf["Reply: generation speed"])

    def test_warmup_setting_validation(self):
        import nova_settings
        self.assertEqual(nova_settings.validate_settings({"model_warmup": "STARTUP"})[0]["model_warmup"], "startup")
        clean, warnings = nova_settings.validate_settings({"model_warmup": "always"})
        self.assertEqual((clean["model_warmup"], len(warnings)), ("typing", 1))


if __name__ == "__main__":
    unittest.main()

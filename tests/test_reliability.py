"""Tests for reliability, security and recovery: logging, atomic writes, safe
folder walking, backups/restore, database failures, Ollama failures,
diagnostics, voice errors, worker threads and shutdown.

Run from the project folder:  python -m unittest discover -s tests -v

Everything uses temporary folders and databases.
"""
import json
import logging
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_backup as nb  # noqa: E402
import nova_diagnostics as nd  # noqa: E402
import nova_settings  # noqa: E402
import nova_system  # noqa: E402


class TempTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nova_rel_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class DbTest(TempTest):
    def setUp(self):
        super().setUp()
        self.db = self.tmp / "nova_memory.db"
        patcher = mock.patch.object(assistant, "DB_PATH", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        assistant.initialize_db()
        self.session = assistant.create_session("Planning")
        assistant.save_message(self.session, "user", "hello")
        assistant.save_memory("Prefers tea", "preference")

    def count(self, table, path=None):
        con = sqlite3.connect(path or self.db)
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()


# ==================== logging ====================
class LoggingTests(TempTest):
    def test_redaction(self):
        cases = {
            "key sk-abcdefghijklmnopqrstuvwxyz123456": "[redacted key]",
            "Authorization: Bearer abcdefghijklmnop": "Bearer [redacted]",
            "password=hunter2 next": "password=[redacted] next",
            "my api_key: 'abc123xyz'": "api_key: [redacted]",
            "OLLAMA_HOST=http://admin:s3cret@10.0.0.5:11434": "http://[redacted]@10.0.0.5",
            "mail me at shiva@example.com": "[email]",
            "card 4111 1111 1111 1111 expired": "[number]",
        }
        for text, expected in cases.items():
            with self.subTest(text):
                result = nova_system.redact(text)
                self.assertIn(expected, result)
        self.assertNotIn("hunter2", nova_system.redact("password: hunter2"))
        self.assertNotIn("s3cret", nova_system.redact("http://admin:s3cret@host"))
        long = nova_system.redact("x" * 10000)
        self.assertLess(len(long), 4100)
        self.assertIn("more characters not logged", long)

    def test_rotating_redacting_file_handler(self):
        path = self.tmp / "nova.log"
        handler = nova_settings.make_log_handler(path, max_bytes=2000, backups=2)
        logger = logging.getLogger("nova.test.rotation")
        logger.propagate = False
        logger.addHandler(handler)
        self.addCleanup(lambda: (logger.removeHandler(handler), handler.close()))
        try:
            raise ValueError("token=SUPERSECRET123")
        except ValueError:
            logger.exception("Failed with password=hunter2")
        for i in range(200):
            logger.warning("line %d %s", i, "y" * 50)
        handler.flush()
        files = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(files, ["nova.log", "nova.log.1", "nova.log.2"], "rotates and keeps at most 2 old files")
        self.assertTrue(all((self.tmp / f).stat().st_size <= 2200 for f in files))
        everything = "".join((self.tmp / f).read_text(encoding="utf-8") for f in files)
        self.assertNotIn("hunter2", everything)
        line = path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING  \[MainThread\] nova\.test\.rotation: line")

    def test_thread_crashes_are_logged(self):
        records = []
        logger = logging.getLogger("nova.test.hooks")
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        saved = (sys.excepthook, threading.excepthook)
        self.addCleanup(lambda: (setattr(sys, "excepthook", saved[0]), setattr(threading, "excepthook", saved[1]),
                                 logger.removeHandler(handler)))
        nova_system.install_exception_hooks(logger)
        thread = threading.Thread(target=lambda: 1 / 0, name="Boom")
        thread.start()
        thread.join()
        self.assertEqual(len(records), 1)
        self.assertIn("Boom", records[0].getMessage())


# ==================== atomic writes and safe walking ====================
class FileSafetyTests(TempTest):
    def test_atomic_write_keeps_old_file_on_failure(self):
        target = self.tmp / "settings.json"
        nova_system.atomic_write_text(target, "old")
        with mock.patch("nova_system.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                nova_system.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["settings.json"], "no temp files left")

    def _junction_tree(self):
        import _winapi
        root = self.tmp / "root"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "notes.txt").write_text("inside", encoding="utf-8")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        _winapi.CreateJunction(str(root), str(root / "docs" / "loop"))
        _winapi.CreateJunction(str(outside), str(root / "docs" / "escape"))
        return root

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows-only")
    def test_safe_walk_never_loops_or_escapes_through_junctions(self):
        root = self._junction_tree()
        visited, files = 0, []
        for dirpath, dirnames, filenames in nova_system.safe_walk(root):
            visited += 1
            files += filenames
            self.assertLess(visited, 10, "walk must terminate")
        self.assertEqual(files, ["notes.txt"])
        self.assertTrue(nova_system.is_redirect(root / "docs" / "loop"))

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows-only")
    def test_file_search_and_document_scan_stay_inside(self):
        import nova_documents
        import nova_gui
        root = self._junction_tree()
        (root / "docs" / "secret-plan.txt").write_text("x", encoding="utf-8")
        matches, _stopped = nova_gui.search_filenames(root, "secret")
        self.assertEqual([p.name for p in matches], ["secret-plan.txt"])
        with mock.patch.object(assistant, "DB_PATH", self.tmp / "docs.db"):
            assistant.initialize_db()
            con = assistant.connect_db()
            try:
                nova_documents.initialize_schema(con)
                source_id, _ = nova_documents.add_source(con, root)
                ids = nova_documents.scan_source(con, source_id)
                names = sorted(r[0] for r in con.execute("SELECT name FROM doc_files"))
            finally:
                con.close()
        self.assertEqual(len(ids), 2)
        self.assertEqual(names, ["notes.txt", "secret-plan.txt"])


# ==================== backups and restore ====================
class BackupTests(DbTest):
    def make(self, settings=None):
        settings_path = None
        if settings is not None:
            settings_path = self.tmp / "nova_settings.json"
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
        return nb.create_backup(self.db, self.tmp / "backups", settings_path), settings_path

    def rewrite(self, backup, manifest=None, members=None, drop=()):
        """Copy a backup with changed manifest/members, to simulate damaged or hostile files."""
        target = self.tmp / f"edited-{len(list(self.tmp.glob('edited-*')))}.novabackup"
        with zipfile.ZipFile(backup) as source, zipfile.ZipFile(target, "w") as out:
            for name in source.namelist():
                if name in drop:
                    continue
                data = source.read(name)
                if name == nb.MANIFEST_MEMBER and manifest is not None:
                    data = manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode()
                out.writestr(name, (members or {}).get(name, data))
            for name, data in (members or {}).items():
                if name not in source.namelist():
                    out.writestr(name, data)
        return target

    def manifest(self, backup):
        with zipfile.ZipFile(backup) as zf:
            return json.loads(zf.read(nb.MANIFEST_MEMBER))

    def test_round_trip_with_safety_backup(self):
        backup, settings_path = self.make({"minimize_to_tray": False, "tts_rate": 150})
        info = nb.inspect_backup(backup)
        self.assertEqual((info.counts["messages"], info.counts["memories"], info.includes_settings), (1, 1, True))
        assistant.save_message(self.session, "user", "written after the backup")
        settings_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(nb.BackupError):
            nb.restore_backup(backup, self.db, self.tmp / "backups", settings_path)   # not confirmed
        self.assertEqual(self.count("messages"), 2, "nothing changes without confirmation")
        result = nb.restore_backup(backup, self.db, self.tmp / "backups", settings_path, confirmed=True)
        self.assertEqual(self.count("messages"), 1)
        self.assertTrue(result.safety_backup.name.startswith("pre-restore-"))
        self.assertEqual(nb.inspect_backup(result.safety_backup).counts["messages"], 2, "undo is possible")
        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual((restored["minimize_to_tray"], restored["tts_rate"]), (False, 150))
        self.assertEqual(nb.check_database(self.db)[:2], (True, "OK"))

    def test_malformed_and_hostile_backups_are_rejected_without_changes(self):
        backup, _ = self.make()
        good = self.manifest(backup)
        not_zip = self.tmp / "not-a-backup.novabackup"
        not_zip.write_bytes(b"PK\x03\x04 broken")
        garbage_db = b"this is not sqlite" * 100

        def with_manifest(**changes):
            data = json.loads(json.dumps(good))
            for key, value in changes.items():
                if "." in key:
                    outer, inner = key.split(".")
                    data[outer][inner] = value
                elif value is ...:
                    data.pop(key)
                else:
                    data[key] = value
            return self.rewrite(backup, manifest=data)

        no_tables = self.tmp / "empty.db"
        sqlite3.connect(no_tables).execute("CREATE TABLE other (x)").connection.commit()
        import hashlib
        empty_bytes = no_tables.read_bytes()
        cases = {
            "not a zip": (not_zip, "isn't a valid Nova backup"),
            "missing database": (self.rewrite(backup, drop=(nb.DB_MEMBER,)), "incomplete"),
            "manifest not JSON": (self.rewrite(backup, manifest=b"{not json"), "manifest is damaged"),
            "manifest is a list": (self.rewrite(backup, manifest=b"[1, 2]"), "isn't a JSON object"),
            "wrong format": (with_manifest(format="something-else"), "isn't a Nova backup"),
            "format version as text": (with_manifest(format_version="1"), "format isn't supported"),
            "newer schema": (with_manifest(schema_version=nb.SCHEMA_VERSION + 1), "newer version of Nova"),
            "bad date": (with_manifest(created_at="yesterday"), "date is invalid"),
            "size mismatch": (with_manifest(**{"database.size": good["database"]["size"] + 1}), "size doesn't match"),
            "bad checksum format": (with_manifest(**{"database.sha256": "abc"}), "checksum is invalid"),
            "checksum mismatch": (with_manifest(**{"database.sha256": "0" * 64}), "doesn't match its checksum"),
            "negative counts": (with_manifest(counts={"messages": -1}), "counts are invalid"),
            "huge manifest": (self.rewrite(backup, manifest=b" " * (nb.MAX_MANIFEST_BYTES + 1)), "too large"),
            "database is garbage": (self.rewrite(backup, manifest={**good, "database": {
                "file": nb.DB_MEMBER, "size": len(garbage_db), "sha256": hashlib.sha256(garbage_db).hexdigest()}},
                members={nb.DB_MEMBER: garbage_db}), "can't be read"),
            "not a Nova database": (self.rewrite(backup, manifest={**good, "database": {
                "file": nb.DB_MEMBER, "size": len(empty_bytes), "sha256": hashlib.sha256(empty_bytes).hexdigest()}},
                members={nb.DB_MEMBER: empty_bytes}), "isn't a Nova database"),
        }
        for name, (path, fragment) in cases.items():
            with self.subTest(name):
                with self.assertRaises(nb.BackupError) as caught:
                    nb.restore_backup(path, self.db, self.tmp / "backups", confirmed=True)
                self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.count("messages"), 1, "the live database was never touched")
        self.assertEqual([p.name for p in (self.tmp / "backups").iterdir()], [backup.name], "no safety backups made")

    def test_archive_paths_cannot_escape(self):
        backup, _ = self.make()
        hostile = self.rewrite(backup, members={"../../escaped.txt": b"x", "C:/Windows/evil.txt": b"x",
                                                "/abs/evil.txt": b"x"})
        nb.restore_backup(hostile, self.db, self.tmp / "backups", confirmed=True)
        self.assertFalse(list(self.tmp.rglob("escaped.txt")))
        self.assertFalse(list(self.tmp.rglob("evil.txt")))

    def test_settings_in_a_backup_are_sanitized(self):
        backup, settings_path = self.make({"tts_rate": 99999, "tts_volume": "loud", "minimize_to_tray": "maybe"})
        nb.restore_backup(backup, self.db, self.tmp / "backups", settings_path, confirmed=True)
        restored = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["tts_rate"], nova_settings.TTS_RATE_MAX)
        self.assertEqual((restored["tts_volume"], restored["minimize_to_tray"]), (1.0, True))

    def test_damaged_live_database_is_kept_aside_when_restoring(self):
        backup, _ = self.make()
        self.db.write_bytes(b"corrupted!" * 500)
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.assertFalse(nb.check_database(self.db)[0])
        result = nb.restore_backup(backup, self.db, self.tmp / "backups", confirmed=True)
        self.assertIsNone(result.safety_backup)
        self.assertEqual(result.damaged_copy.read_bytes()[:10], b"corrupted!")
        self.assertEqual(self.count("messages"), 1)

    def test_set_aside_and_check_database(self):
        self.assertEqual(nb.check_database(self.tmp / "missing.db")[0], True)
        con = sqlite3.connect(self.db)
        con.execute(f"PRAGMA user_version = {nb.SCHEMA_VERSION + 5}")
        con.commit()
        con.close()
        healthy, message, version = nb.check_database(self.db)
        self.assertTrue(healthy)
        self.assertIn("newer version of Nova", message)
        self.db.write_bytes(b"\x00garbage" * 300)
        self.assertFalse(nb.check_database(self.db)[0])
        moved = nb.set_aside_damaged(self.db)
        self.assertFalse(self.db.exists())
        self.assertTrue(moved.exists())

    def test_auto_backup_daily_and_pruned(self):
        folder = self.tmp / "auto"
        start = datetime(2026, 11, 1, 9, 0)
        first = nb.auto_backup(self.db, folder, now=start)
        self.assertIsNotNone(first)
        os.utime(first, (start.timestamp(), start.timestamp()))
        self.assertIsNone(nb.auto_backup(self.db, folder, now=start + timedelta(hours=5)), "once a day")
        for day in range(1, 10):
            moment = start + timedelta(days=day)
            path = nb.auto_backup(self.db, folder, now=moment)
            os.utime(path, (moment.timestamp(), moment.timestamp()))
        autos = [p for p, _w, _s in nb.list_backups(folder)]
        self.assertEqual(len(autos), nb.AUTO_KEEP, "older automatic backups are pruned")
        newest = datetime.fromtimestamp(autos[0].stat().st_mtime)
        self.assertEqual(newest, start + timedelta(days=9))

    def test_backup_of_a_damaged_database_is_refused(self):
        self.db.write_bytes(b"not a database" * 100)
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        with self.assertRaises(nb.BackupError) as caught:
            nb.create_backup(self.db, self.tmp / "backups")
        self.assertIn("wasn't backed up", str(caught.exception))
        self.assertFalse(list((self.tmp / "backups").glob("*.novabackup")) if (self.tmp / "backups").exists() else [])

    def test_schema_version_stamp(self):
        con = assistant.connect_db()
        try:
            nb.stamp_schema_version(con)
            self.assertEqual(nb.schema_version(con), nb.SCHEMA_VERSION)
            con.execute("PRAGMA user_version = 99")
            nb.stamp_schema_version(con)
            self.assertEqual(nb.schema_version(con), 99, "never lowered")
        finally:
            con.close()


# ==================== database failures ====================
class DatabaseFailureTests(DbTest):
    def test_transaction_rolls_back_and_closes(self):
        with self.assertRaises(RuntimeError):
            with assistant.transaction() as conn:
                conn.execute("INSERT INTO memories (category, content, created_at, updated_at) VALUES ('a', 'b', 'c', 'd')")
                raise RuntimeError("crash mid-way")
        self.assertEqual(self.count("memories"), 1, "the partial insert was rolled back")
        with assistant.transaction() as conn:
            pass
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_unwritable_database_surfaces_a_clear_error(self):
        import nova_gui
        app = mock.Mock()
        app.events = queue.Queue()
        with mock.patch.object(nova_gui, "save_message_safe", side_effect=sqlite3.OperationalError("database is locked")):
            nova_gui.NovaGUI._generation_worker(app, self.session, "hi", False, "gemma3:4b")
        event, message = app.events.get_nowait()
        self.assertEqual(event, "generation_error")
        self.assertIn("couldn't save to its database", message)
        self.assertNotIn("Ollama", message)


# ==================== Ollama failures and diagnostics ====================
class OllamaAndDiagnosticsTests(DbTest):
    def test_ollama_error_messages(self):
        import ollama
        missing = ollama.ResponseError("model 'llama9' not found", 404)
        self.assertIn("isn't installed", nd.describe_ollama_error(missing, "llama9"))
        self.assertIn("ollama pull llama9", nd.describe_ollama_error(missing, "llama9"))
        self.assertIn("couldn't reach Ollama", nd.describe_ollama_error(ConnectionError("refused"), "x"))
        self.assertIn("out of memory", nd.describe_ollama_error(ollama.ResponseError("boom", 500), "x"))

    def test_generation_reports_unavailable_ollama(self):
        import nova_gui
        app = mock.Mock()
        app.events = queue.Queue()
        with mock.patch.object(nova_gui.ollama, "chat", side_effect=ConnectionError("refused")):
            nova_gui.NovaGUI._generation_worker(app, self.session, "hi", False, "gemma3:4b")
        events = [app.events.get_nowait() for _ in range(app.events.qsize())]
        self.assertEqual(events[-1][0], "generation_error")
        self.assertIn("couldn't reach Ollama", events[-1][1])

    def run_checks(self, **kwargs):
        defaults = dict(db_path=self.db, model="gemma3:4b", backup_folder=self.tmp / "backups",
                        log_path=self.tmp / "nova.log")
        defaults.update(kwargs)
        return {(c.area, c.name): c for c in nd.run_checks(**defaults)}

    def test_diagnostics_never_raise(self):
        from nova_settings import OllamaUnavailable
        checks = self.run_checks(list_models=mock.Mock(side_effect=OllamaUnavailable("Couldn't reach Ollama on this PC.")),
                                 find_whisper=mock.Mock(side_effect=assistant.VoiceInputError("not found")),
                                 find_vosk_model=lambda: None, count_input_devices=mock.Mock(side_effect=OSError("no audio")))
        self.assertEqual(checks[("Ollama", "Connection")].status, nd.ERROR)
        self.assertIn("Couldn't reach Ollama", checks[("Ollama", "Connection")].detail)
        self.assertEqual(checks[("Speech", "whisper.cpp (voice input)")].status, nd.WARNING)
        self.assertEqual(checks[("Speech", "Microphones")].status, nd.WARNING)
        self.assertEqual(checks[("Database", "Integrity")].status, nd.OK)
        self.assertEqual(checks[("Backups", "Latest")].status, nd.WARNING)

    def test_missing_model_and_remote_host(self):
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "http://admin:hunter2@192.168.1.50:11434"}):
            checks = self.run_checks(list_models=lambda: ["llama3.2:3b"])
            report = nd.report_text(list(checks.values()))
        self.assertEqual(checks[("Ollama", "Selected model")].status, nd.ERROR)
        self.assertEqual(checks[("Ollama", "Address")].status, nd.WARNING)
        self.assertIn("another computer", checks[("Ollama", "Address")].detail)
        self.assertNotIn("hunter2", report)
        self.assertNotIn("admin", report)

    def test_missing_optional_and_required_components(self):
        def fake(import_name, distribution):
            return (False, None) if import_name in ("vosk", "ollama") else (True, "1.0")
        with mock.patch.object(nd, "component_status", side_effect=fake):
            checks = self.run_checks()
        self.assertEqual(checks[("Components", "vosk")].status, nd.WARNING)
        self.assertIn("pip install vosk", checks[("Components", "vosk")].detail)
        self.assertEqual(checks[("Components", "ollama")].status, nd.ERROR)

    def test_damaged_database_is_reported(self):
        self.db.write_bytes(b"bad" * 400)
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.assertEqual(self.run_checks()[("Database", "Integrity")].status, nd.ERROR)

    def test_invalid_settings_are_reported_and_defaulted(self):
        path = self.tmp / "nova_settings.json"
        path.write_text(json.dumps({"tts_rate": "fast", "minimize_to_tray": 7, "ollama_model": ["x"]}), encoding="utf-8")
        settings, warnings = nova_settings.load_settings(path)
        self.assertEqual((settings["tts_rate"], settings["minimize_to_tray"], settings["ollama_model"]),
                         (None, True, None))
        checks = self.run_checks(settings_warnings=warnings)
        self.assertEqual(checks[("Settings", "Settings file")].status, nd.WARNING)


# ==================== voice and speech failures ====================
class VoiceFailureTests(unittest.TestCase):
    def test_microphone_errors_become_readable(self):
        if assistant.sd is None:
            self.skipTest("sounddevice not installed")
        with mock.patch.object(assistant.sd, "InputStream", side_effect=assistant.sd.PortAudioError("Device unavailable")):
            with self.assertRaises(assistant.VoiceInputError) as caught:
                assistant.record_until_silence(lambda _s: None)
        self.assertIn("Couldn't use the microphone", str(caught.exception))
        with mock.patch.object(assistant.sd, "InputStream", side_effect=ValueError("No input device matching 99")):
            with self.assertRaises(assistant.VoiceInputError):
                assistant.record_until_silence(lambda _s: None)

    def test_recording_can_be_stopped(self):
        if assistant.sd is None:
            self.skipTest("sounddevice not installed")

        class SilentStream:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        stop = threading.Event()
        stop.set()
        with mock.patch.object(assistant.sd, "InputStream", SilentStream):
            with self.assertRaises(assistant.VoiceInputError) as caught:
                assistant.record_until_silence(lambda _s: None, stop)
        self.assertIn("stopped", str(caught.exception))

    def test_missing_voice_packages(self):
        with mock.patch.object(assistant, "np", None):
            with self.assertRaises(assistant.VoiceInputError) as caught:
                assistant.record_until_silence(lambda _s: None)
        self.assertIn("pip install numpy", str(caught.exception))
        with mock.patch.object(assistant, "pyttsx3", None):
            self.assertIsNone(assistant.initialize_voice_engine())
            with self.assertRaises(RuntimeError):
                assistant.speak_text("hello")
            with self.assertRaises(RuntimeError):
                assistant.list_voices()

    def test_speech_failure_is_reported(self):
        engine = mock.Mock()
        engine.runAndWait.side_effect = RuntimeError("SAPI died")
        with mock.patch.object(assistant, "initialize_voice_engine", return_value=engine):
            with self.assertRaises(RuntimeError) as caught:
                assistant.speak_text("hello")
        self.assertIn("Speech output failed", str(caught.exception))


# ==================== workers and shutdown ====================
class WorkerAndShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import nova_gui
        cls.gui = nova_gui

    def app(self):
        app = mock.Mock()
        app._exiting = False
        app._jobs = {}
        app.root.after.side_effect = lambda ms, fn: f"job-{ms}-{id(fn)}"
        return app

    def test_one_pending_timer_per_name(self):
        app = self.app()
        schedule = lambda *a: self.gui.NovaGUI._schedule(app, *a)
        schedule("events", 100, lambda: None)
        first = app._jobs["events"]
        schedule("events", 100, lambda: None)
        app.root.after_cancel.assert_called_once_with(first)
        self.assertEqual(len(app._jobs), 1)
        app._exiting = True
        schedule("other", 100, lambda: None)
        self.assertNotIn("other", app._jobs, "nothing new is scheduled while exiting")

    def test_wake_listener_never_runs_twice(self):
        app = self.app()
        app.wake_active = True
        app.wake_listener_running.return_value = True     # the old listener hasn't stopped yet
        app._schedule = mock.Mock()
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI._start_wake_thread(app)
        thread.assert_not_called()
        app._schedule.assert_called_once()
        app.wake_listener_running.return_value = False
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI._start_wake_thread(app)
        thread.assert_called_once()

    def test_shutdown_stops_everything(self):
        app = self.app()
        app._jobs = {"events": "job1", "reminders": "job2"}
        cancel = threading.Event()
        app._task_ui = {"state": {"cancel": cancel}}
        stopping = threading.Event()
        worker = threading.Thread(target=stopping.wait, args=(5,), name="FakeWake")
        worker.start()
        app.wake_thread, app.voice_thread = worker, None
        app.wake_stop_event = mock.Mock(set=stopping.set)
        app.voice_stop_event = threading.Event()
        alert = mock.Mock()
        app._alert_windows = [alert]
        tray = app.tray_icon
        started = time.monotonic()
        self.gui.NovaGUI._shutdown(app)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(worker.is_alive(), "the wake-word thread was stopped and joined")
        self.assertEqual(app.root.after_cancel.call_count, 2)
        self.assertEqual(app._jobs, {})
        app.indexer.stop.assert_called_once()
        self.assertTrue(cancel.is_set())
        self.assertTrue(app.voice_stop_event.is_set())
        alert.destroy.assert_called_once()
        tray.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()

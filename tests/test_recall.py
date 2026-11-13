"""Tests for advanced memory and context retrieval (Step 32): migration of existing databases and backward
compatibility, privacy (secrets, “don't remember”), conflicting facts, retrieval relevance and ranking,
work entries, task continuity with the agent engine (verified resumption, no repeated actions), recall
before planning, outcome recording, export and deletion, corrupted-database recovery, the chat context,
and the chat commands.

Everything runs on temporary databases and scratch folders; the model is scripted.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_agent as na  # noqa: E402
import nova_backup as nb  # noqa: E402
import nova_context as nc  # noqa: E402
import nova_memories as nm  # noqa: E402
import nova_recall as nr  # noqa: E402
from test_agent import AgentTest, FakeModel, plan, step  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import migrate_memory  # noqa: E402

# The memories table as it was before Step 32 (Step 9's columns included), with sessions and messages.
OLD_SCHEMA = """
CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT 'New conversation',
                       created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, role TEXT NOT NULL,
                       content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL DEFAULT 'general',
                       content TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                       pinned INTEGER NOT NULL DEFAULT 0, include_in_prompt INTEGER NOT NULL DEFAULT 1,
                       source TEXT NOT NULL DEFAULT 'manual', use_count INTEGER NOT NULL DEFAULT 0,
                       last_used_at TEXT, reviewed_at TEXT, UNIQUE(category, content));
PRAGMA user_version = 24;
"""


def old_database(folder, memories=(("preference", "I prefer short answers"), ("profile", "I live in Pune"))):
    path = Path(folder) / "nova_memory.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    for i, (category, content) in enumerate(memories, start=1):
        con.execute("INSERT INTO memories (category, content, created_at, updated_at, pinned) VALUES (?, ?, ?, ?, ?)",
                    (category, content, "2026-01-05T10:00:00+05:30", "2026-01-05T10:00:00+05:30", int(i == 1)))
    con.execute("INSERT INTO sessions (title, created_at, updated_at) VALUES ('Chat', 'x', 'x')")
    con.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (1, 'user', 'hello', 'x')")
    con.commit()
    con.close()
    return path


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="nova_recall_"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        patcher = mock.patch.object(assistant, "DB_PATH", self.folder / "nova.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        assistant.initialize_db()
        self.con = assistant.connect_db()
        self.addCleanup(self.con.close)
        nc.ensure_schema(self.con)                      # Step 24's tables (chat summaries, projects)


# ==================== migration and backward compatibility ====================
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="nova_recall_mig_"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_existing_memories_survive_unchanged_and_a_backup_is_made_first(self):
        path = old_database(self.folder)
        before = sqlite3.connect(path).execute("SELECT id, category, content, created_at, updated_at, pinned "
                                               "FROM memories ORDER BY id").fetchall()
        con = sqlite3.connect(path)
        added = nm.migrate(con, path)
        self.assertEqual(added, ["confidence", "source_ref", "superseded_by"])
        self.assertTrue(list(self.folder.glob("nova_memory.backup-*.db")), "backed up before the change")
        after = con.execute("SELECT id, category, content, created_at, updated_at, pinned FROM memories "
                            "ORDER BY id").fetchall()
        self.assertEqual(before, after)
        memories = nm.all_memories(con)
        self.assertEqual({m.confidence for m in memories}, {"user_stated"})
        self.assertTrue(all(m.superseded_by is None for m in memories))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"work_threads", "work_entries", "memory_blocklist", "memory_changes"} <= tables)
        self.assertEqual(nm.migrate(con, path), [], "running it again changes nothing")
        # The existing memory API works on the migrated rows exactly as before.
        block, included = nm.memory_context(con, "where do I live?")
        self.assertIn("I live in Pune", block)
        self.assertEqual(nm.list_memories(con, "pune")[0].content, "I live in Pune")
        con.close()

    def test_migration_script(self):
        path = old_database(self.folder)
        out = []
        self.assertEqual(migrate_memory.migrate(path, check_only=True, out=out.append), 0)
        self.assertIn("--check: nothing was changed.", out)
        con = sqlite3.connect(path)
        self.assertNotIn("confidence", {r[1] for r in con.execute("PRAGMA table_info(memories)")})
        con.close()
        out.clear()
        self.assertEqual(migrate_memory.migrate(path, backup_dir=self.folder / "b", out=out.append), 0)
        self.assertTrue(any(line.startswith("Done and verified") for line in out), out)
        self.assertEqual(len(list((self.folder / "b").glob("pre-memory-upgrade-*.db"))), 1)
        con = sqlite3.connect(path)
        self.assertEqual(nb.schema_version(con), nb.SCHEMA_VERSION)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 2)
        con.close()
        out.clear()
        self.assertEqual(migrate_memory.migrate(path, out=out.append), 0)
        self.assertIn("Already up to date: nothing to change.", out)

    def test_damaged_databases_are_never_changed(self):
        path = self.folder / "nova_memory.db"
        path.write_bytes(b"SQLite format 3\x00" + b"\x13" * 4000)
        before = path.read_bytes()
        out = []
        self.assertEqual(migrate_memory.migrate(path, out=out.append), 1)
        self.assertIn("Not changed", out[0])
        self.assertEqual(path.read_bytes(), before)


class RecoveryTests(DatabaseTest):
    def test_corrupted_database_is_set_aside_and_a_backup_restores_memory(self):
        nm.add_memory(self.con, "I prefer tea", "preference")
        thread = nr.thread_for(self.con, "NOVA development")
        nr.add_entry(self.con, thread.id, "decision", "Keep memory in the existing SQLite database")
        backups = self.folder / "backups"
        backup = nb.create_backup(assistant.DB_PATH, backups)
        self.con.close()
        Path(assistant.DB_PATH).write_bytes(b"this is not a database" * 100)
        ok, message, _version = nb.check_database(assistant.DB_PATH)
        self.assertFalse(ok)
        moved = nb.set_aside_damaged(assistant.DB_PATH)
        self.assertTrue(Path(moved).exists(), "the damaged file is kept, never deleted")
        assistant.initialize_db()                                          # Nova starts fresh
        con = assistant.connect_db()
        self.assertEqual(nm.counts(con)["all"], 0)
        con.close()
        nb.restore_backup(backup, assistant.DB_PATH, backups, confirmed=True)
        con = assistant.connect_db()
        self.addCleanup(con.close)
        self.assertEqual([m.content for m in nm.all_memories(con)], ["I prefer tea"])
        self.assertEqual([e.content for e in nr.entries(con)], ["Keep memory in the existing SQLite database"])

    def test_a_damaged_record_stays_readable(self):
        thread = nr.thread_for(self.con, "NOVA development")
        entry, _ = nr.add_entry(self.con, thread.id, "next", "Copy the notes", action={"tool": "copy_file",
                                                                                       "args": {}})
        with self.con:
            self.con.execute("UPDATE work_entries SET action = '{not json' WHERE id = ?", (entry.id,))
        reloaded = nr.get_entry(self.con, entry.id)
        self.assertIsNone(reloaded.action, "it just can't be resumed automatically")
        self.assertEqual(reloaded.content, "Copy the notes")


# ==================== privacy ====================
class PrivacyTests(DatabaseTest):
    def test_secrets_are_never_stored(self):
        for text in ("My password is hunter2!", "my wifi password: Tr0ub4dor&3", "API key = sk-abcdefghijklmnop1234",
                     "My card is 4111 1111 1111 1111", "PIN code is 4932"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    nm.add_memory(self.con, text)
                thread = nr.thread_for(self.con, "Accounts")
                with self.assertRaises(nr.MemoryRefused):
                    nr.add_entry(self.con, thread.id, "fact", text)
        nm.add_memory(self.con, "My password manager is Bitwarden")              # an ordinary fact
        self.assertEqual(nm.suggest_from_message(self.con, "My name is Sam. My password is hunter2"), [])

    def test_dont_remember_a_topic(self):
        nm.add_memory(self.con, "I see Dr. Rao about my health every March", "health")
        nm.add_memory(self.con, "I prefer tea")
        nr.block(self.con, "health")
        with self.assertRaises(ValueError):
            nm.add_memory(self.con, "My health insurance renews in June")
        thread = nr.thread_for(self.con, "Errands")
        with self.assertRaises(nr.MemoryRefused):
            nr.add_entry(self.con, thread.id, "next", "Book the health check-up")
        self.assertEqual([m.content for m in nr.usable_memories(self.con)], ["I prefer tea"])
        block, _ = nm.memory_context(self.con, "health appointment in March")
        self.assertNotIn("Dr. Rao", block, "kept, but never sent")
        self.assertEqual(nm.counts(self.con)["all"], 2, "nothing was deleted without asking")
        session = assistant.create_session()
        assistant.save_message(session, "user", "my health is fine")
        self.assertEqual(nr.purge_blocked(self.con, "health"), (1, 0))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1, "chats are untouched")
        self.assertTrue(nr.unblock(self.con, "health"))
        nm.add_memory(self.con, "My health insurance renews in June")

    def test_changes_are_logged_without_contents(self):
        memory = nm.add_memory(self.con, "I prefer tea").memory
        nm.delete_memory(self.con, memory.id)
        log = nr.recent_changes(self.con)
        self.assertEqual([(r["action"], r["item_type"]) for r in log[:2]], [("delete", "memory"), ("add", "memory")])
        self.assertNotIn("tea", json.dumps(log))


# ==================== conflicting facts ====================
class ConflictTests(DatabaseTest):
    def test_newer_fact_supersedes_without_deleting(self):
        older = nm.add_memory(self.con, "My favourite colour is blue").memory
        newer = nm.add_memory(self.con, "My favorite colour is green").memory
        self.assertEqual([(a.id, b.id) for a, b in nr.conflicts(self.con)], [(older.id, newer.id)])
        replaced = nr.supersede_on_save(self.con, newer)
        self.assertEqual([m.id for m in replaced], [older.id])
        self.assertEqual(nm.get_memory(self.con, older.id).superseded_by, newer.id)
        block, _ = nm.memory_context(self.con, "what is my favourite colour?")
        self.assertIn("green", block)
        self.assertNotIn("blue", block)
        self.assertEqual(nm.counts(self.con)["all"], 2)
        self.assertEqual(nr.conflicts(self.con), [])
        nr.restore_superseded(self.con, older.id)
        self.assertEqual(len(nr.conflicts(self.con)), 1)
        nm.keep_both(self.con, older.id, newer.id)
        self.assertEqual(nr.conflicts(self.con), [], "the user said both are right")

    def test_compatible_facts_are_not_conflicts(self):
        nm.add_memory(self.con, "My favourite colour is blue")
        nm.add_memory(self.con, "My favourite food is dosa")
        nm.add_memory(self.con, "I prefer tea")
        self.assertEqual(nr.conflicts(self.con), [])

    def test_deleting_the_newer_fact_brings_the_older_back(self):
        older = nm.add_memory(self.con, "I live in Pune").memory
        newer = nm.add_memory(self.con, "I live in Mumbai").memory
        nr.supersede_on_save(self.con, newer)
        nm.delete_memory(self.con, newer.id)
        self.assertIsNone(nm.get_memory(self.con, older.id).superseded_by)


# ==================== retrieval ====================
class RetrievalTests(DatabaseTest):
    def seed(self):
        nova = nr.thread_for(self.con, "NOVA development")
        garden = nr.thread_for(self.con, "Garden")
        nr.add_entry(self.con, nova.id, "decision", "Keep the work memory in the existing SQLite database file")
        nr.add_entry(self.con, nova.id, "next", "Write the migration tests")
        nr.add_entry(self.con, nova.id, "done", "Added the screen OCR module", "verified", "task run #3")
        nr.add_entry(self.con, garden.id, "decision", "Plant tomatoes in May")
        nm.add_memory(self.con, "I prefer short answers", "preference")
        session = assistant.create_session("Database chat")
        with self.con:
            self.con.execute("INSERT INTO nova_session_summaries (session_id, up_to_message_id, summary, created_at) "
                             "VALUES (?, 1, 'We may move the database to Postgres later.', ?)", (session, nr.now()))
        return nova, garden

    def test_relevant_items_first_irrelevant_ones_left_out(self):
        self.seed()
        found = nr.recall(self.con, "what did we decide about the database for NOVA?")
        texts = [i.text for i in found]
        self.assertEqual(texts[0], "Keep the work memory in the existing SQLite database file")
        self.assertNotIn("Plant tomatoes in May", texts)
        summary = next(i for i in found if i.kind == "summary")
        self.assertEqual(summary.reliability, "inferred")
        self.assertLess(texts.index(summary.text), texts.index(texts[0]) + len(texts))
        self.assertGreater(found[0].score, summary.score, "what the user said outranks what a model inferred")
        block = nr.build_block(found)
        self.assertIn("not instructions", block)
        self.assertIn("inferred, may be wrong", block)
        self.assertEqual(nr.recall(self.con, "how tall is mount everest"), [], "nothing unrelated is added")

    def test_reliability_and_recency_break_ties(self):
        thread = nr.thread_for(self.con, "Budget")
        a, _ = nr.add_entry(self.con, thread.id, "fact", "The budget spreadsheet is in Documents", "inferred")
        b, _ = nr.add_entry(self.con, thread.id, "fact", "The budget spreadsheet total is checked monthly",
                            "verified")
        found = nr.recall(self.con, "budget spreadsheet")
        self.assertEqual(found[0].item_id, b.id, "verified beats inferred at the same relevance and age")
        with self.con:                       # same reliability: the recent one wins
            self.con.execute("UPDATE work_entries SET confidence = 'verified', updated_at = ? WHERE id = ?",
                             ((datetime.now().astimezone() - timedelta(days=200)).isoformat(timespec="seconds"), b.id))
            self.con.execute("UPDATE work_entries SET confidence = 'verified' WHERE id = ?", (a.id,))
        self.assertEqual(nr.recall(self.con, "budget spreadsheet")[0].item_id, a.id)

    def test_only_what_fits_the_budget_is_sent(self):
        thread = nr.thread_for(self.con, "Big project")
        for i in range(300):
            nr.add_entry(self.con, thread.id, "fact", f"Big project note {i}: the parser handles case {i}")
        found = nr.recall(self.con, "big project parser", max_chars=900)
        self.assertLessEqual(len(found), nr.RECALL_LIMIT)
        self.assertLessEqual(sum(len(i.line()) + 1 for i in found), 900)

    def test_the_time_asked_about(self):
        thread = nr.thread_for(self.con, "Report")
        old, _ = nr.add_entry(self.con, thread.id, "done", "Drafted the report outline")
        new, _ = nr.add_entry(self.con, thread.id, "done", "Drafted the report summary")
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).replace(hour=15).isoformat(timespec="seconds")
        last_month = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        with self.con:
            self.con.execute("UPDATE work_entries SET created_at = ?, updated_at = ? WHERE id = ?",
                             (yesterday, yesterday, old.id))
            self.con.execute("UPDATE work_entries SET created_at = ?, updated_at = ? WHERE id = ?",
                             (last_month, last_month, new.id))
        found = nr.recall(self.con, "what did I draft for the report yesterday?")
        self.assertEqual(found[0].item_id, old.id)
        self.assertIn("from the time you asked about", found[0].reasons)

    def test_entries_dedupe_and_done_closes_next(self):
        thread = nr.thread_for(self.con, "NOVA development")
        first, created = nr.add_entry(self.con, thread.id, "next", "Write the migration tests")
        again, created_again = nr.add_entry(self.con, thread.id, "next", "write the migration tests.")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, again.id)
        nr.add_entry(self.con, thread.id, "done", "Write the migration tests")
        self.assertEqual(nr.get_entry(self.con, first.id).status, "resolved")

    def test_temporary_context_expires_but_is_kept(self):
        thread = nr.thread_for(self.con, "NOVA development")
        entry, _ = nr.add_entry(self.con, thread.id, "blocker", "Copy failed: file in use", durable=False)
        self.assertIsNotNone(entry.expires_at)
        later = datetime.now().astimezone() + timedelta(days=nr.TEMPORARY_DAYS + 1)
        self.assertEqual(nr.expire(self.con, later), 1)
        self.assertEqual(nr.get_entry(self.con, entry.id).status, "expired")
        self.assertEqual(nr.recall(self.con, "copy failed file in use"), [], "expired context isn't recalled")

    def test_commands(self):
        self.assertEqual(nr.parse_work_command("decision: use SQLite"), ("decision", None, "use SQLite"))
        self.assertEqual(nr.parse_work_command("next (NOVA development): write tests"),
                         ("next", "NOVA development", "write tests"))
        self.assertEqual(nr.parse_work_command("blocked on the API key: waiting for access"),
                         ("blocker", "the API key", "waiting for access"))
        self.assertIsNone(nr.parse_work_command("note: Groceries | eggs"), "Step 7's note command is unchanged")
        self.assertEqual(nr.parse_no_store("Please don't remember anything about my health."), "my health")
        self.assertEqual(nr.parse_allow_store("you can remember my health again"), "my health")
        self.assertEqual(nr.parse_status("What's the status of NOVA development?"), "NOVA development")
        for text, what in (("Continue the NOVA development from yesterday", "NOVA development from yesterday"),
                           ("pick up where we left off", ""), ("resume the archive task", "archive task")):
            self.assertEqual(nr.parse_continue(text), what, text)
        for text in ("continue typing", "keep going with the story", "what time is it"):
            self.assertIsNone(nr.parse_continue(text), text)
        window = nr.parse_window("from yesterday")
        self.assertEqual((window[1] - window[0]).days, 1)


# ==================== the agent: recall, recording, continuity ====================
class ContinuityTests(AgentTest):
    def setUp(self):
        super().setUp()
        self.con = assistant.connect_db()
        self.addCleanup(self.con.close)
        self.thread = nr.thread_for(self.con, "Archive work")
        self.archive = self.allowed / "Archive"

    def run_agent(self, request, model, limits=None, stop_after=None, memory=True):
        env = na.Env(services=self.services, settings={}, connect=assistant.connect_db, approved=set())
        cancel = threading.Event()

        def on_event(kind, data):
            self.events.append(kind)
            if kind == "step" and stop_after and data.id == stop_after and data.status == "succeeded":
                cancel.set()                                   # the user presses Stop after this step

        engine = na.AgentEngine(
            na.default_registry(), na.Planner(model, "gemma3:4b", timeout=5), env, assistant.connect_db,
            limits=dict({"max_retries": 0}, **(limits or {})), on_event=on_event,
            approve_plan=lambda p: self.asked.append(("plan", [s.tool for s in p.steps], list(p.notes))) or True,
            approve_step=lambda s, prep: True, approve_folders=lambda s, f: True, answer=False,
            sleep=lambda s: None, memory=nr.AgentMemory(assistant.connect_db) if memory else None)
        return engine.run(request, cancel)

    def first_run(self):
        """Archive work, stopped by the user after its first step, so two steps are left."""
        model = FakeModel(plan(step("s1", "create_folder", {"folder": str(self.allowed), "name": "Archive"}),
                               step("s2", "copy_file", {"file": str(self.allowed / "notes.txt"),
                                                        "destination": str(self.archive)}),
                               step("s3", "create_file", {"folder": str(self.archive), "name": "report.txt",
                                                          "content": "private report text"})))
        return self.run_agent(f"archive work: in {self.allowed} create an Archive folder, copy notes.txt into it "
                              "and write a report", model, stop_after="s1")

    def test_unfinished_work_is_recorded_with_verified_state(self):
        report = self.first_run()
        self.assertEqual(report.status, "cancelled")
        rows = {(e.kind, e.status) for e in nr.entries(self.con, self.thread.id)}
        self.assertIn(("done", "resolved"), rows)
        done = nr.entries(self.con, self.thread.id, kinds=("done",))[0]
        self.assertEqual(done.confidence, "verified")
        self.assertIn("checked on disk", done.content)
        nexts = nr.entries(self.con, self.thread.id, kinds=("next",))
        self.assertEqual(len(nexts), 2)
        self.assertEqual(nexts[0].action["tool"], "copy_file")
        self.assertIsNone(nexts[1].action, "a step with private text can't be redone automatically")
        self.assertNotIn("private report text", json.dumps([e.__dict__ for e in nr.entries(self.con)]))
        self.assertTrue(all(not e.durable for e in nexts), "an interrupted run's leftovers are temporary context")

    def test_continue_resumes_only_what_is_left_after_checking(self):
        self.first_run()
        model = FakeModel()                                         # never asked: the plan comes from memory
        report = self.run_agent("Continue the archive work from yesterday", model)
        self.assertEqual(model.calls, [])
        self.assertEqual(report.status, "succeeded", report.text())
        self.assertEqual([s.tool for s in report.steps], ["copy_file"], "the folder isn't created twice")
        plan_notes = self.asked[-1][2]
        self.assertTrue(any("Still to do (needs you or a new plan)" in n for n in plan_notes))
        self.assertTrue((self.archive / "notes.txt").is_file())
        copy_next = nr.entries(self.con, self.thread.id, kinds=("next",))[0]
        self.assertEqual(copy_next.status, "resolved")
        # Continuing again repeats nothing: the copy is done now.
        report = self.run_agent("continue the archive work", FakeModel())
        self.assertEqual(report.steps, [])
        self.assertIn("nothing was run", report.answer)

    def test_actions_done_in_the_meantime_are_not_repeated(self):
        self.first_run()
        # The copy is done another way that doesn't update work memory (e.g. a plain task run). Continuing checks
        # the task history itself, finds it, and closes the step instead of copying again.
        self.run_agent("copy notes.txt into the archive", FakeModel(plan(step(
            "s1", "copy_file", {"file": str(self.allowed / "notes.txt"), "destination": str(self.archive)}))),
            memory=False)
        (self.archive / "notes.txt").unlink()            # even if the result was removed later: it was done
        report = self.run_agent("continue the archive work", FakeModel())
        self.assertEqual(report.steps, [])
        self.assertIn("Already done, so not repeated: Copy", report.answer)
        self.assertFalse((self.archive / "notes.txt").exists(), "nothing was redone")
        closed = nr.entries(self.con, self.thread.id, kinds=("next",))[0]
        self.assertEqual(closed.status, "resolved")
        self.assertRegex(closed.resolution, r"done since \(task run #\d+\)")

    def test_actions_recorded_as_done_close_the_open_step(self):
        self.first_run()
        self.run_agent("copy notes.txt into the archive", FakeModel(plan(step(
            "s1", "copy_file", {"file": str(self.allowed / "notes.txt"), "destination": str(self.archive)}))))
        report = self.run_agent("continue the archive work", FakeModel())
        self.assertEqual(report.steps, [])
        self.assertEqual(nr.entries(self.con, self.thread.id, kinds=("next",))[0].status, "resolved")

    def test_steps_that_no_longer_make_sense_are_reported_not_run(self):
        self.first_run()
        (self.allowed / "notes.txt").unlink()
        report = self.run_agent("continue the archive work", FakeModel())
        self.assertEqual(report.steps, [])
        self.assertIn("Can't resume", report.answer)

    def test_nothing_recorded_means_nothing_resumed(self):
        model = FakeModel()
        report = self.run_agent("continue the moon landing project", model)
        self.assertEqual(report.status, "rejected")
        self.assertIn("no recorded work", report.text())
        self.assertEqual(model.calls, [])

    def test_memory_reaches_the_planner_only_when_relevant(self):
        garden = nr.thread_for(self.con, "Garden")
        nr.add_entry(self.con, garden.id, "decision", "Water the tomatoes with the blue can every evening")
        model = FakeModel(plan())
        self.run_agent("how should I water the tomatoes in the garden?", model)
        prompt = model.calls[0]["messages"][1]["content"]
        self.assertIn("Recalled from Nova's memory", prompt)
        self.assertIn("blue can", prompt)
        model = FakeModel(plan())
        self.run_agent("what's the capital of peru and the time there?", model)
        self.assertNotIn("Recalled from Nova's memory", model.calls[0]["messages"][1]["content"])

    def test_read_only_runs_store_nothing(self):
        self.run_agent("find invoice files in downloads", FakeModel(plan(step(
            "s1", "search_files", {"term": "invoice", "folder": "downloads"}))))
        self.assertEqual(nr.entries(self.con), [])


# ==================== chat context, export, deletion, commands ====================
class ChatAndManagementTests(DatabaseTest):
    def test_chat_context_includes_matching_work_only(self):
        thread = nr.thread_for(self.con, "NOVA development")
        nr.add_entry(self.con, thread.id, "decision", "Use Windows OCR for screen text")
        session = assistant.create_session()
        assistant.save_message(session, "user", "which OCR did we pick for screen text?")
        ctx = nc.build(self.con, session, "which OCR did we pick for screen text?")
        self.assertIn("Use Windows OCR for screen text", ctx.reference_message())
        self.assertIn("<work_memory>", ctx.reference_message())
        assistant.save_message(session, "user", "tell me a joke about cats")
        ctx = nc.build(self.con, session, "tell me a joke about cats")
        self.assertNotIn("work_memory", ctx.reference_message())
        with mock.patch.object(nr, "RECALL_IN_CHAT", False):
            ctx = nc.build(self.con, session, "which OCR did we pick for screen text?")
            self.assertNotIn("work_memory", ctx.reference_message())

    def test_export_and_delete(self):
        nm.add_memory(self.con, "I prefer tea")
        thread = nr.thread_for(self.con, "NOVA development")
        entry, _ = nr.add_entry(self.con, thread.id, "next", "Write docs")
        session = assistant.create_session()
        assistant.save_message(session, "user", "secret chat text")
        data = nr.export_all(self.con)
        self.assertEqual(data["memories"][0]["content"], "I prefer tea")
        self.assertEqual(data["work"][0]["entries"][0]["content"], "Write docs")
        self.assertNotIn("secret chat text", json.dumps(data), "chats aren't part of the memory export")
        nr.update_entry(self.con, entry.id, "Write the user docs")
        self.assertEqual(nr.get_entry(self.con, entry.id).content, "Write the user docs")
        self.assertTrue(nr.delete_entry(self.con, entry.id))
        self.assertTrue(nr.delete_thread(self.con, thread.id))
        self.assertEqual(nr.list_threads(self.con), [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_chat_commands(self):
        import nova_gui
        app = mock.Mock()
        reply = nova_gui.NovaGUI._memory_command(app, None, "decision (NOVA development): keep SQLite")
        self.assertIn("Decision noted under “NOVA development”", reply)
        reply = nova_gui.NovaGUI._memory_command(app, None, "next (NOVA development): write the README")
        self.assertIn("continue NOVA development", reply)
        brief = nova_gui.NovaGUI._memory_command(app, None, "what's the status of NOVA development?")
        self.assertIn("keep SQLite", brief)
        self.assertIn("write the README", brief)
        reply = nova_gui.NovaGUI._memory_command(app, None, "next: my password is hunter2")
        self.assertIn("didn't record", reply)
        reply = nova_gui.NovaGUI._memory_command(app, None, "don't remember anything about my salary")
        self.assertIn("won't remember anything about “my salary”", reply)
        self.assertEqual(nr.blocklist(self.con), ["my salary"])
        self.assertIsNone(nova_gui.NovaGUI._memory_command(app, None, "what's the weather like?"))


class MemoriesWindowTests(AgentTest):
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

    def test_work_and_privacy_tab(self):
        con = assistant.connect_db()
        self.addCleanup(con.close)
        thread = nr.thread_for(con, "NOVA development")
        nr.add_entry(con, thread.id, "next", "Write the README section", "verified", "task run #7")
        nm.add_memory(con, "My favourite colour is blue")
        nm.add_memory(con, "My favourite colour is green")
        nr.block(con, "salary")
        self.app.open_memories()
        self.tk.update()
        labels = [w.cget("text") for w in self._walk(self.app._memory_window) if w.__class__.__name__ == "CTkLabel"]
        for expected in ("NOVA development", "Write the README section", "salary"):
            self.assertIn(expected, labels)
        self.assertTrue(any("Older: “My favourite colour is blue”" in t for t in labels))


if __name__ == "__main__":
    unittest.main()

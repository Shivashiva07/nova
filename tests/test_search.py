"""Tests for global conversation search (nova_search + nova_gui glue).

Run from the project folder:  python -m unittest discover -s tests -v

Every test uses a temporary database created with assistant.initialize_db(),
so the real nova_memory.db is never opened.
"""
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import assistant  # noqa: E402
import nova_search  # noqa: E402
from nova_search import make_excerpt, parse_query, search  # noqa: E402


class SearchDatabaseTest(unittest.TestCase):
    """Base class: a fresh database with a few known conversations."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Cleanups run last-in-first-out: this one runs after the DB is closed,
        # which Windows needs before the file can be deleted.
        self.addCleanup(self.tmp.cleanup)
        patcher =mock.patch.object(assistant, "DB_PATH", Path(self.tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        assistant.initialize_db()

        self.python = assistant.create_session("Python help")
        self.say(self.python, "user", "How do I fix a Python ImportError?")
        self.say(self.python, "assistant", "Check that the **package** is installed with pip install requests.")
        self.cooking = assistant.create_session("Dinner ideas")
        self.say(self.cooking, "user", "Give me a quick pasta recipe")
        self.say(self.cooking, "assistant", "Try spaghetti aglio e olio: garlic, olive oil and chili.")
        self.symbols = assistant.create_session("Symbols")
        self.say(self.symbols, "user", "Discount is 50% off for user_name! today")
        self.con = assistant.connect_db()
        self.addCleanup(self.con.close)

    def say(self, session_id, role, content):
        # Save directly so the session title isn't overwritten by the first message.
        with assistant.connect_db() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, assistant.now()),
            )

    def contents(self, page):
        return [hit.content for hit in page.messages]


class ParseQueryTests(unittest.TestCase):
    def test_words_and_phrases(self):
        self.assertEqual(parse_query('  python   "import error"  fix '), ["python", "import error", "fix"])

    def test_empty_and_blank(self):
        self.assertEqual(parse_query(""), [])
        self.assertEqual(parse_query("   \n\t "), [])
        self.assertEqual(parse_query('""'), [])
        self.assertEqual(parse_query(None), [])

    def test_case_insensitive_duplicates_dropped(self):
        self.assertEqual(parse_query("Python python PYTHON"), ["Python"])

    def test_limits(self):
        self.assertEqual(len(parse_query(" ".join(f"w{i}" for i in range(50)))), nova_search.MAX_TERMS)
        self.assertEqual(len(parse_query("x" * 500)[0]), nova_search.MAX_TERM_LENGTH)

    def test_like_pattern_escapes_wildcards(self):
        self.assertEqual(nova_search.like_pattern("50%_a!"), "%50!%!_a!!%")


class SearchTests(SearchDatabaseTest):
    def test_existing_word(self):
        page = search(self.con, "pasta")
        self.assertEqual(self.contents(page), ["Give me a quick pasta recipe"])
        hit = page.messages[0]
        self.assertEqual((hit.session_id, hit.role, hit.title), (self.cooking, "user", "Dinner ideas"))
        self.assertTrue(hit.created_at)

    def test_partial_word(self):
        self.assertEqual(len(search(self.con, "spagh").messages), 1)
        self.assertEqual(len(search(self.con, "porterr").messages), 1)  # inside "ImportError"

    def test_case_insensitive(self):
        self.assertEqual(len(search(self.con, "IMPORTERROR").messages), 1)
        self.assertEqual(len(search(self.con, "garLIC").messages), 1)

    def test_multiple_keywords_all_required_any_order(self):
        self.assertEqual(self.contents(search(self.con, "pip requests")),
                         ["Check that the **package** is installed with pip install requests."])
        self.assertEqual(self.contents(search(self.con, "requests pip")),
                         ["Check that the **package** is installed with pip install requests."])
        self.assertEqual(search(self.con, "pip pasta").messages, [])

    def test_phrase(self):
        self.assertEqual(len(search(self.con, '"olive oil"').messages), 1)
        self.assertEqual(search(self.con, '"oil olive"').messages, [])

    def test_empty_query(self):
        page = search(self.con, "   ")
        self.assertEqual((page.terms, page.titles, page.messages, page.has_more), ([], [], [], False))

    def test_no_matches(self):
        page = search(self.con, "xylophone")
        self.assertEqual((page.titles, page.messages, page.has_more), ([], [], False))

    def test_wildcards_are_literal(self):
        self.assertEqual(len(search(self.con, "50%").messages), 1)
        self.assertEqual(search(self.con, "%").messages[0].session_id, self.symbols)
        self.assertEqual(len(search(self.con, "user_name!").messages), 1)
        self.assertEqual(search(self.con, "user_nam_").messages, [])  # _ isn't "any character"

    def test_injection_attempts_are_plain_text(self):
        before = self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        for attack in ("'; DROP TABLE messages; --", "\" OR 1=1 --", "') OR ('1'='1", "x' ESCAPE '"):
            page = search(self.con, attack)
            self.assertEqual(page.messages, [], attack)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], before)

    def test_titles(self):
        page = search(self.con, "dinner")
        self.assertEqual([(t.session_id, t.title, t.message_count) for t in page.titles],
                         [(self.cooking, "Dinner ideas", 2)])
        self.assertEqual(page.messages, [])
        # Titles need every word too.
        self.assertEqual(search(self.con, "python help").titles[0].session_id, self.python)
        self.assertEqual(search(self.con, "python dinner").titles, [])

    def test_search_is_read_only(self):
        changes = self.con.total_changes
        search(self.con, "python")
        search(self.con, "nothing here")
        self.assertEqual(self.con.total_changes, changes)

    def test_session_exists(self):
        self.assertTrue(nova_search.session_exists(self.con, self.python))
        self.assertFalse(nova_search.session_exists(self.con, 999999))


class PaginationTests(SearchDatabaseTest):
    def test_many_messages_paged_without_duplicates(self):
        big = assistant.create_session("Long chat")
        with assistant.connect_db() as conn:
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(big, "user" if i % 2 else "assistant", f"Step {i}: tune the widget layout", assistant.now())
                 for i in range(237)],
            )
        seen, before, pages = [], None, 0
        while True:
            page = search(self.con, "widget layout", before_id=before)
            pages += 1
            self.assertLessEqual(len(page.messages), nova_search.PAGE_SIZE)
            if before is not None:
                self.assertEqual(page.titles, [], "titles are only on the first page")
            seen.extend(hit.message_id for hit in page.messages)
            if not page.has_more:
                break
            before = page.next_before_id
        self.assertEqual(pages, 5)
        self.assertEqual(len(seen), 237)
        self.assertEqual(len(set(seen)), 237)
        self.assertEqual(seen, sorted(seen, reverse=True), "newest first")

    def test_exact_page_size_has_no_extra_page(self):
        s = assistant.create_session("Fifty")
        with assistant.connect_db() as conn:
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(s, "user", f"marker {i}", assistant.now()) for i in range(nova_search.PAGE_SIZE)],
            )
        page = search(self.con, "marker")
        self.assertEqual((len(page.messages), page.has_more), (nova_search.PAGE_SIZE, False))

    def test_large_history_stays_fast(self):
        s = assistant.create_session("Bulk")
        filler = "lorem ipsum dolor sit amet " * 20
        with assistant.connect_db() as conn:
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(s, "assistant", f"{filler} item{i}", assistant.now()) for i in range(20000)],
            )
        started = time.perf_counter()
        common = search(self.con, "lorem")              # stops after one page
        missing = search(self.con, "nonexistentword")   # worst case: scans everything
        elapsed = time.perf_counter() - started
        self.assertEqual(len(common.messages), nova_search.PAGE_SIZE)
        self.assertTrue(common.has_more)
        self.assertEqual(missing.messages, [])
        self.assertLess(elapsed, 2.0, f"search took {elapsed:.2f}s")


class ExcerptTests(unittest.TestCase):
    def test_short_text_highlighted(self):
        excerpt, spans = make_excerpt("Fix the Python ImportError", ["python", "error"])
        self.assertEqual(excerpt, "Fix the Python ImportError")
        self.assertEqual([excerpt[a:b] for a, b in spans], ["Python", "Error"])

    def test_long_text_centres_on_match_with_ellipses(self):
        content = ("alpha " * 100) + "the needle is here " + ("omega " * 100)
        excerpt, spans = make_excerpt(content, ["needle"], length=80)
        self.assertTrue(excerpt.startswith("…") and excerpt.endswith("…"))
        self.assertIn("needle", excerpt)
        self.assertLessEqual(len(excerpt), 82)
        self.assertEqual([excerpt[a:b] for a, b in spans], ["needle"])
        self.assertNotIn("alph…", excerpt)  # cut at word boundaries

    def test_whitespace_collapsed_and_unicode_highlight(self):
        excerpt, spans = make_excerpt("Ünïcode\n\n  TEXT", ["ünï", "text"])
        self.assertEqual(excerpt, "Ünïcode TEXT")
        self.assertEqual([excerpt[a:b] for a, b in spans], ["Ünï", "TEXT"])

    def test_markdown_markers_removed(self):
        excerpt, spans = make_excerpt("## Tip\nUse **zebra crossings** and run `pip install`\n> quoted", ["zebra", "pip"])
        self.assertEqual(excerpt, "Tip Use zebra crossings and run pip install quoted")
        self.assertEqual([excerpt[a:b] for a, b in spans], ["zebra", "pip"])

    def test_no_match_and_empty(self):
        self.assertEqual(make_excerpt("", ["x"]), ("", []))
        excerpt, spans = make_excerpt("word " * 100, ["zzz"], length=50)
        self.assertTrue(excerpt.startswith("word") and excerpt.endswith("…"))
        self.assertEqual(spans, [])


class GuiGlueTests(SearchDatabaseTest):
    """nova_gui's database helpers used by the search window (no window is opened)."""

    @classmethod
    def setUpClass(cls):
        import nova_gui
        cls.gui = nova_gui

    def test_fetch_message_rows_includes_ids_and_order(self):
        rows = self.gui.fetch_message_rows(self.python)
        self.assertEqual([r[1] for r in rows], ["user", "assistant"])
        self.assertLess(rows[0][0], rows[1][0])
        self.assertEqual(self.gui.fetch_messages(self.python), [(r[1], r[2]) for r in rows])

    def test_search_conversations_and_exists(self):
        page = self.gui.search_conversations("garlic")
        self.assertEqual(page.messages[0].session_id, self.cooking)
        self.assertTrue(self.gui.conversation_exists(self.cooking))
        assistant.delete_session(self.cooking)
        self.assertFalse(self.gui.conversation_exists(self.cooking))

    def test_database_error_is_raised_as_sqlite_error(self):
        with mock.patch.object(assistant, "DB_PATH", Path(self.tmp.name) / "missing" / "nope.db"):
            with self.assertRaises(sqlite3.Error):
                self.gui.search_conversations("python")


if __name__ == "__main__":
    unittest.main()

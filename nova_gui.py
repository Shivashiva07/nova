"""
Nova AI — local Windows desktop GUI
Place this file in the same folder as assistant.py, then run:
    python nova_gui.py

Uses the existing local Ollama + SQLite + whisper.cpp setup.
No cloud APIs are used.
"""
from __future__ import annotations

import os
import sys
import time

_PROCESS_STARTED = time.perf_counter()
from datetime import datetime, timedelta, timezone
import re
import shutil
import sqlite3
import webbrowser
import json
import queue
import threading
import subprocess
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

def _fatal(message):
    """Show a startup error in a window (pythonw.exe has no console), then exit."""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Nova can't start", message, parent=root)
        root.destroy()
    except Exception:
        pass
    raise SystemExit(message)


import importlib.util

if importlib.util.find_spec("ollama") is None:
    _fatal("The ollama package is missing. In Nova's virtual environment run: pip install ollama")

OLLAMA_CONNECT_TIMEOUT = 5      # seconds to reach Ollama
OLLAMA_READ_TIMEOUT = 300       # seconds without any data (a cold model load can take a while)


def _configure_ollama(module):
    """Give Ollama requests timeouts (by default they could wait forever)."""
    import httpx
    module.chat.__self__._client.timeout = httpx.Timeout(OLLAMA_READ_TIMEOUT, connect=OLLAMA_CONNECT_TIMEOUT)


from nova_system import LazyModule  # noqa: E402

# Imported on first use (or in the background after start-up): ~0.35 s saved at launch.
ollama = LazyModule("ollama", on_load=_configure_ollama)

try:
    import assistant as backend
except Exception as exc:
    _fatal("Could not load assistant.py. Keep nova_gui.py in the same folder as assistant.py and use Nova's "
           f"virtual environment (pip install -r requirements.txt).\n\nDetails: {exc}")

try:
    import customtkinter as ctk
except Exception as exc:
    _fatal(f"CustomTkinter is missing. In Nova's virtual environment run: pip install customtkinter\n\n{exc}")

from nova_widgets import (
    ACCENT, ACCENT_HOVER, ACCENT_SOFT, BG, BORDER, CODE_BG, DANGER, DANGER_SOFT, FOUND_TERM_BG, MUTED,
    SELECTION, SIDEBAR, SUBTLE, SUCCESS, SURFACE, SURFACE_2, TEXT, WARNING,
    ChatView, Fonts, Icon, NavButton, SessionList, StatusPill, Tooltip,
    autohide_scrollbar, bind_tree, fit_text, icon_button, set_cursor,
)

import nova_actions
import nova_agent
import nova_backup
import nova_context
import nova_diagnostics
import nova_documents
import nova_memories
import nova_online
import nova_perf
import nova_reminders
import nova_tasks
import nova_search
import nova_settings
import nova_voice
from nova_settings import SETTINGS_PATH, log
from nova_system import NOVA_VERSION, atomic_write_text, safe_walk

APP_TITLE = "Nova"
# Defaults from assistant.py, used whenever a setting is left at "Default".
DEFAULT_MODEL = getattr(backend, "MODEL", "gemma3:4b")
DEFAULT_MICROPHONE = getattr(backend, "MICROPHONE_DEVICE", 1)
DEFAULT_TTS_RATE = getattr(backend, "TTS_RATE", 175)

STARTUP_SCRIPT_NAME = "NovaAI.vbs"
BACKUP_DIR = Path(__file__).resolve().with_name("backups")


def load_nova_settings():
    """Return (settings, warnings) from nova_settings.json; never raises."""
    return nova_settings.load_settings(SETTINGS_PATH)


def save_nova_settings(settings):
    """Validate and save settings; returns the saved dict. Raises OSError."""
    return nova_settings.save_settings(settings, SETTINGS_PATH)


def active_model():
    return getattr(backend, "MODEL", DEFAULT_MODEL) or DEFAULT_MODEL


def apply_runtime_settings(settings, devices=None):
    """Apply model, voice and microphone settings to the running backend.

    Takes effect immediately: the next reply, spoken response and recording
    use the new values. Returns user-facing notes about anything that had to
    fall back to a default (e.g. an unplugged microphone).
    """
    notes = []
    backend.MODEL = settings.get("ollama_model") or DEFAULT_MODEL
    backend.TTS_VOICE = settings.get("tts_voice")
    backend.TTS_RATE = settings.get("tts_rate") or DEFAULT_TTS_RATE
    volume = settings.get("tts_volume")
    backend.TTS_VOLUME = 1.0 if volume is None else volume

    name = settings.get("microphone_name")
    device = DEFAULT_MICROPHONE
    if name:
        if devices is None:
            try:
                devices = nova_settings.list_input_devices()
            except RuntimeError as exc:
                log.error("Microphone lookup failed: %s", exc)
                devices = []
        index = nova_settings.resolve_input_device(name, settings.get("microphone_hostapi"), devices)
        if index is None:
            log.warning("Saved microphone %r (%s) not found", name, settings.get("microphone_hostapi"))
            notes.append(
                f"The microphone “{name}” isn't connected, so Nova is using the default "
                f"input device ({DEFAULT_MICROPHONE}). Choose another one in Settings › Voice."
            )
        else:
            device = index
    backend.MICROPHONE_DEVICE = device
    log.info(
        "Runtime settings applied: model=%s mic=%s rate=%s volume=%s voice=%s",
        backend.MODEL, device, backend.TTS_RATE, backend.TTS_VOLUME, bool(backend.TTS_VOICE),
    )
    return notes


# Hands-free voice (Step 29): one state machine and one speaker for the whole app.
VOICE_STATE = nova_voice.VoiceStateMachine()
SPEAKER = nova_voice.Speaker(lambda text, stop_event: backend.speak_text(text, stop_event=stop_event), VOICE_STATE)


def speak_text(text):
    """Speak a reply with the configured voice settings; “Stop speaking” ends it. Raises on failure."""
    SPEAKER.speak(text)


def phrase_label(settings):
    """The wake phrase as shown to the user, e.g. “Hey Nova”."""
    phrase = str(settings.get("wake_phrase") or "")
    if nova_voice.WakePhrase.check(phrase) is not None:
        phrase = nova_voice.DEFAULT_PHRASE
    return " ".join(w.capitalize() for w in phrase.split())


def wake_word_support():
    """Return None if "Hey Nova" can run, else a user-facing reason it can't."""
    try:
        import vosk  # noqa: F401
    except Exception:
        return "Needs Vosk: run “pip install vosk” in Nova's virtual environment, then restart Nova."
    if find_vosk_model() is None:
        return "Needs the Vosk model: extract vosk-model-small-en-us-0.15 into the Nova folder (see README)."
    return None


def get_startup_script_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_SCRIPT_NAME


def startup_enabled():
    path = get_startup_script_path()
    return bool(path and path.exists())


def set_startup_enabled(enabled):
    """Install/remove a per-user, hidden VBS launcher in the Windows Startup folder."""
    path = get_startup_script_path()
    if path is None:
        raise RuntimeError("The Windows APPDATA folder could not be located.")
    if enabled:
        python_exe = Path(sys.executable).resolve()
        pythonw = python_exe.with_name("pythonw.exe")
        launcher = pythonw if pythonw.exists() else python_exe
        script = Path(__file__).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # VBS quoting: double any embedded quote characters in Windows paths.
        launcher_q = str(launcher).replace('"', '""')
        script_q = str(script).replace('"', '""')
        work_q = str(script.parent).replace('"', '""')
        vbs = (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.CurrentDirectory = "{work_q}"\n'
            f'WshShell.Run """{launcher_q}"" ""{script_q}""", 0, False\n'
        )
        atomic_write_text(path, vbs)
    elif path.exists():
        path.unlink()
    return path


def load_tray_dependencies():
    try:
        import pystray
        from PIL import Image, ImageDraw
        return pystray, Image, ImageDraw
    except Exception:
        return None


def call_backend(name, *args, default=None, **kwargs):
    fn = getattr(backend, name, None)
    if callable(fn):
        return fn(*args, **kwargs)
    return default


def initialize_database():
    call_backend("initialize_db")


def current_session_id():
    sid = call_backend("latest_session")
    if sid is None:
        sid = call_backend("create_session")
    # Accommodate implementations that return a row or a tuple.
    if hasattr(sid, "keys"):
        for key in ("id", "session_id"):
            try:
                if key in sid.keys():
                    return sid[key]
            except Exception:
                pass
    if isinstance(sid, (tuple, list)):
        return sid[0] if sid else None
    return sid


def new_session_id():
    sid = call_backend("create_session")
    if hasattr(sid, "keys"):
        for key in ("id", "session_id"):
            try:
                if key in sid.keys():
                    return sid[key]
            except Exception:
                pass
    if isinstance(sid, (tuple, list)):
        return sid[0] if sid else None
    return sid


def get_db_connection():
    fn = getattr(backend, "connect_db", None)
    if not callable(fn):
        raise RuntimeError("assistant.py does not expose connect_db().")
    return fn()


def fetch_sessions(project=nova_context.ALL):
    """(id, title, last activity) newest first, for all chats or one project's (None: no project)."""
    if project != nova_context.ALL:
        try:
            rows = with_db(nova_context.list_sessions, project)
        except sqlite3.Error as exc:
            log.warning("Couldn't list the project's chats: %s", exc)
            rows = []
        return [(sid, str(title or f"Chat {sid}"), str(updated or "")) for sid, title, updated in rows]
    result = call_backend("list_sessions", default=[])
    if result is None:
        return []
    if isinstance(result, list):
        rows = result
    else:
        try:
            rows = list(result)
        except Exception:
            rows = []
    cleaned = []
    for row in rows:
        try:
            if hasattr(row, "keys"):
                sid = row["id"] if "id" in row.keys() else row["session_id"]
                label = row["title"] if "title" in row.keys() else None
                # backend.list_sessions() returns updated_at (last activity) and
                # sorts by it; fall back to created_at for other schemas.
                keys = row.keys()
                created = (
                    row["updated_at"] if "updated_at" in keys
                    else row["created_at"] if "created_at" in keys
                    else ""
                )
            elif isinstance(row, (tuple, list)):
                sid = row[0]
                label = row[1] if len(row) > 1 else None
                created = row[2] if len(row) > 2 else ""
            else:
                sid, label, created = row, None, ""
            cleaned.append((sid, str(label or f"Chat {sid}"), str(created or "")))
        except Exception:
            continue
    return cleaned


def fetch_message_rows(session_id):
    """Return the full (message_id, role, content) history of a conversation, oldest first.

    message_id is None if the table has no id column. Don't use
    backend.load_context() here: it is capped at MAX_CONTEXT_MESSAGES for the
    model's context window, which would truncate the displayed chat.
    """
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("PRAGMA table_info(messages)")
        cols = {r[1] for r in cur.fetchall()}
        session_col = "session_id" if "session_id" in cols else None
        role_col = "role" if "role" in cols else None
        content_col = "content" if "content" in cols else None
        id_expr = '"id"' if "id" in cols else "NULL"
        order_col = "id" if "id" in cols else ("created_at" if "created_at" in cols else None)
        if not all((session_col, role_col, content_col)):
            return []
        query = f'SELECT {id_expr}, "{role_col}", "{content_col}" FROM messages WHERE "{session_col}" = ?'
        if order_col:
            query += f' ORDER BY "{order_col}" ASC'
        cur.execute(query, (session_id,))
        return [(r[0], r[1], r[2]) for r in cur.fetchall() if r[1] in ("user", "assistant")]
    finally:
        con.close()


def fetch_messages(session_id):
    """Return the full (role, content) history of a conversation, oldest first."""
    return [(role, content) for _id, role, content in fetch_message_rows(session_id)]


CHAT_PAGE = 60   # messages drawn when a chat opens; older ones load on request


def fetch_message_window(session_id, limit=CHAT_PAGE, anchor_id=None, context=5):
    """A slice of a conversation, oldest first: (rows, earlier_count, later_count).

    Without anchor_id: the latest `limit` messages. With anchor_id: `limit`
    messages starting `context` messages before it (for search results).
    Only this slice is read from the database.
    """
    con = get_db_connection()
    try:
        if anchor_id is None:
            rows = con.execute("SELECT id, role, content FROM messages WHERE session_id = ? "
                               "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()[::-1]
        else:
            before = con.execute("SELECT id FROM messages WHERE session_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                                 (session_id, anchor_id, context)).fetchall()
            first = before[-1][0] if before else anchor_id
            rows = con.execute("SELECT id, role, content FROM messages WHERE session_id = ? AND id >= ? "
                               "ORDER BY id LIMIT ?", (session_id, first, limit)).fetchall()
        if not rows:
            return [], 0, 0
        earlier = con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND id < ?",
                              (session_id, rows[0][0])).fetchone()[0]
        later = con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ? AND id > ?",
                            (session_id, rows[-1][0])).fetchone()[0]
        return [(r[0], r[1], r[2]) for r in rows if r[1] in ("user", "assistant")], earlier, later
    finally:
        con.close()


def message_id_before(session_id, message_id, count):
    """The id `count` messages before message_id (or the first message)."""
    con = get_db_connection()
    try:
        row = con.execute("SELECT id FROM messages WHERE session_id = ? AND id < ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                          (session_id, message_id, max(0, count - 1))).fetchone()
        if row:
            return row[0]
        row = con.execute("SELECT MIN(id) FROM messages WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row and row[0] is not None else message_id
    finally:
        con.close()


def count_messages(session_id):
    con = get_db_connection()
    try:
        return con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
    finally:
        con.close()


def has_messages(session_id):
    con = get_db_connection()
    try:
        return con.execute("SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (session_id,)).fetchone() is not None
    finally:
        con.close()


def recent_messages(session_id, limit):
    """The last `limit` (role, content) pairs, oldest first, for the model's context."""
    con = get_db_connection()
    try:
        rows = con.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                           (session_id, int(limit))).fetchall()
        return [(r[0], r[1]) for r in reversed(rows) if r[0] in ("user", "assistant")]
    finally:
        con.close()


def _chunk_done(chunk):
    try:
        return bool(chunk["done"])
    except Exception:
        return bool(getattr(chunk, "done", False))


def _chunk_stat(chunk, name):
    try:
        value = chunk[name]
    except Exception:
        value = getattr(chunk, name, None)
    return value or 0


def record_reply_timings(prefix, started, first_token_at, final_chunk):
    """Store latency numbers for the Diagnostics page (no content)."""
    if first_token_at is not None:
        nova_perf.record(f"{prefix}: time to first word", first_token_at * 1000)
    nova_perf.record(f"{prefix}: total time", (time.perf_counter() - started) * 1000)
    if final_chunk is not None:
        count, duration = _chunk_stat(final_chunk, "eval_count"), _chunk_stat(final_chunk, "eval_duration")
        if count and duration:
            nova_perf.record(f"{prefix}: generation speed", count / (duration / 1e9), "tokens/s")
        load = _chunk_stat(final_chunk, "load_duration")
        if load and load > 200e6:   # the model had to be loaded into memory first
            nova_perf.record("Ollama: loading the model into memory", load / 1e6)


def close_ollama_connections():
    """End requests still waiting for Ollama (e.g. while it loads the model), so no reader
    thread is left behind at shutdown. Later requests get a fresh connection pool."""
    if not ollama.loaded():
        return
    try:
        import httpx
        client = ollama.chat.__self__
        old = client._client
        client._client = httpx.Client(base_url=old.base_url, headers=old.headers, timeout=old.timeout)
        old.close()
    except Exception as exc:
        log.debug("Closing the Ollama connections failed: %s", exc)


def _cancel_event(app):
    event = getattr(app, "_cancel_reply", None)
    return event if isinstance(event, threading.Event) else threading.Event()


def cancellable_stream(stream, cancel, poll=0.1):
    """Yield chunks from a blocking Ollama stream, but stop as soon as cancel is set,
    even while Ollama is still loading the model and hasn't sent anything yet.

    A reader thread owns the stream (a generator can't be closed from another
    thread while it's waiting) and closes it when abandoned, which ends the
    HTTP request so Ollama stops generating.
    """
    chunks, abandon = queue.Queue(), threading.Event()

    def reader():
        try:
            for chunk in stream:
                if abandon.is_set() or cancel.is_set():
                    break
                chunks.put(("chunk", chunk))
            chunks.put(("end", None))
        except Exception as exc:
            chunks.put(("error", exc))
        finally:
            close = getattr(stream, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass

    threading.Thread(target=reader, daemon=True, name="NovaModelStream").start()
    try:
        while True:
            try:
                kind, value = chunks.get(timeout=poll)
            except queue.Empty:
                if cancel.is_set():
                    return
                continue
            if kind == "chunk":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    finally:
        abandon.set()


_VOSK_MODELS = {}
_VOSK_LOCK = threading.Lock()


def load_vosk_model(path):
    """The Vosk model, loaded once per session (about 1 s each time otherwise)."""
    from vosk import Model
    key = str(path)
    with _VOSK_LOCK:
        model = _VOSK_MODELS.get(key)
        if model is None:
            started = time.perf_counter()
            model = _VOSK_MODELS[key] = Model(key)
            nova_perf.record("wake word: loading the Vosk model", (time.perf_counter() - started) * 1000)
        return model


def search_conversations(query, before_id=None):
    """Run a global search on a fresh connection (safe to call from a worker thread)."""
    con = get_db_connection()
    try:
        return nova_search.search(con, query, before_id=before_id)
    finally:
        con.close()


def conversation_exists(session_id):
    con = get_db_connection()
    try:
        return nova_search.session_exists(con, session_id)
    finally:
        con.close()


def with_db(fn, *args, **kwargs):
    """Run fn(connection, ...) on a fresh connection (usable from any thread)."""
    con = get_db_connection()
    try:
        return fn(con, *args, **kwargs)
    finally:
        con.close()


def local_timestamp():
    """Local time with UTC offset, same format as the backend's messages/memories."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_timestamp(value):
    """Display a stored timestamp as local 'YYYY-MM-DD HH:MM'.

    Handles ISO values with an offset (current format) and naive
    'YYYY-MM-DD HH:MM:SS' values, which SQLite's CURRENT_TIMESTAMP wrote in UTC.
    """
    if not value:
        return ""
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text[:16]
    if parsed.tzinfo is None:
        if "T" in text:
            return parsed.strftime("%Y-%m-%d %H:%M")  # naive local value
        parsed = parsed.replace(tzinfo=timezone.utc)  # legacy CURRENT_TIMESTAMP
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def _migrate_utc_timestamps(cur):
    """One-time conversion of legacy UTC CURRENT_TIMESTAMP values to local time.

    Converted values contain 'T', so running this again changes nothing.
    """
    columns = {
        "nova_notes": ("created_at", "updated_at"),
        "nova_reminders": ("created_at", "notified_at"),
    }
    for table, cols in columns.items():
        for col in cols:
            rows = cur.execute(
                f"SELECT id, {col} FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} NOT LIKE '%T%'"
            ).fetchall()
            for row_id, value in rows:
                try:
                    utc = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                local = utc.replace(tzinfo=timezone.utc).astimezone()
                cur.execute(
                    f"UPDATE {table} SET {col} = ? WHERE id = ?",
                    (local.isoformat(timespec="seconds"), row_id),
                )


def initialize_productivity_tables():
    """Create Nova's notes/reminders tables without changing existing tables."""
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nova_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nova_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                due_at TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notified_at TEXT
            )
        """)
        _migrate_utc_timestamps(cur)
        con.commit()
        # Recurrence/snooze columns and the alert log (additive; backs up first).
        nova_reminders.migrate(con, getattr(backend, "DB_PATH", None), backup=nova_memories.backup_database)
    finally:
        con.close()


def initialize_document_tables():
    """Create the document index tables (additive; existing tables are untouched)."""
    con = get_db_connection()
    try:
        return nova_documents.initialize_schema(con)
    finally:
        con.close()


def _stream_piece(chunk):
    """Text of one streamed Ollama chunk (dict or object form)."""
    try:
        return chunk["message"]["content"] or ""
    except Exception:
        try:
            return chunk.message.content or ""
        except Exception:
            return ""


def list_notes_db():
    """Notes newest first: (id, title, content, created_at, updated_at, project_id)."""
    con = get_db_connection()
    try:
        project = "project_id" if nova_context._has_column(con, "nova_notes", "project_id") else "NULL"
        return con.execute(
            f"SELECT id, title, content, created_at, updated_at, {project} "
            "FROM nova_notes ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    finally:
        con.close()


_KEEP = object()


def save_note_db(title, content, note_id=None, project_id=_KEEP):
    """Create or update a note. project_id: the note's project (None: no project); omitted keeps it."""
    con = get_db_connection()
    try:
        cur = con.cursor()
        timestamp = local_timestamp()
        if note_id is None:
            cur.execute(
                "INSERT INTO nova_notes (title, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (title.strip(), content.strip(), timestamp, timestamp),
            )
        else:
            cur.execute(
                "UPDATE nova_notes SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                (title.strip(), content.strip(), timestamp, note_id),
            )
        saved_id = cur.lastrowid if note_id is None else note_id
        if project_id is not _KEEP and nova_context._has_column(con, "nova_notes", "project_id"):
            cur.execute("UPDATE nova_notes SET project_id = ? WHERE id = ?",
                        (None if project_id is None else int(project_id), saved_id))
        con.commit()
        return saved_id
    finally:
        con.close()


def delete_note_db(note_id):
    con = get_db_connection()
    try:
        cur = con.execute("DELETE FROM nova_notes WHERE id = ?", (note_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def list_reminders_db(include_completed=False):
    con = get_db_connection()
    try:
        if include_completed:
            return con.execute(
                "SELECT id, title, due_at, completed, created_at, notified_at "
                "FROM nova_reminders ORDER BY completed ASC, due_at ASC, id DESC"
            ).fetchall()
        return con.execute(
            "SELECT id, title, due_at, completed, created_at, notified_at "
            "FROM nova_reminders WHERE completed = 0 ORDER BY due_at ASC, id DESC"
        ).fetchall()
    finally:
        con.close()


def save_reminder_db(title, due_at):
    """Save a one-off reminder at "YYYY-MM-DD HH:MM" local time; returns its id.

    An identical active reminder is reused rather than duplicated or overwritten.
    """
    schedule = nova_reminders.Schedule(nova_reminders.parse_stamp(due_at))
    return with_db(nova_reminders.create, title, schedule).reminder.id


def complete_reminder_db(reminder_id):
    return with_db(nova_reminders.complete, reminder_id)


def delete_reminder_db(reminder_id):
    return with_db(nova_reminders.delete, reminder_id)


REMINDER_EXAMPLES = (
    "Try: “remind me tomorrow at 9 AM to study”, “remind me in 45 minutes to take a break”, "
    "“remind me every Monday at 8 PM to revise SQL” or “remind me on the 15th of every month at 9 AM "
    "to review expenses”."
)
PENDING_REMINDER_MINUTES = 10


def _reminder_outcome(result, now):
    """(reply, pending question or None) for a parsed reminder request."""
    if result.kind == "ok":
        saved = with_db(nova_reminders.create, result.title, result.schedule, now)
        reply = nova_reminders.saved_message(saved, now)
        if saved.created and result.schedule.notes:
            reply += f" (Note: {', '.join(result.schedule.notes)}.)"
        log.info("Reminder %s via chat (%s)", "saved" if saved.created else "already existed",
                 result.schedule.rule or "one-off")
        return reply, None
    if result.kind == "clarify":
        return result.message + "\n\nJust reply with the answer, or say “cancel”.", (result.clarify, now)
    if result.kind == "cancelled":
        return result.message, None
    return f"I couldn't set that reminder: {result.message}\n\n{REMINDER_EXAMPLES}", None


def answer_pending_reminder(text, pending, now=None):
    """If text answers Nova's last reminder question, finish it: (reply, pending). Otherwise None."""
    if not isinstance(pending, tuple) or len(pending) != 2:
        return None
    clarify, asked_at = pending
    now = now or datetime.now()
    if now - asked_at > timedelta(minutes=PENDING_REMINDER_MINUTES):
        return None
    result = nova_reminders.answer(clarify, text, now)
    return None if result is None else _reminder_outcome(result, now)


def new_reminder_request(text, now=None):
    """Handle 'remind me …': (reply, pending) or None if it isn't a reminder request."""
    now = now or datetime.now()
    result = nova_reminders.parse_reminder(text, now)
    return None if result.kind == "none" else _reminder_outcome(result, now)



def handle_productivity_command(text, project_id=None):
    """
    Handle a deliberately small, predictable set of local productivity commands.
    Returns a response string when handled, otherwise None so normal chat can run.
    """
    raw = str(text).strip()
    lowered = raw.lower()

    # List local notes.
    if re.fullmatch(r"(show|list|open|view)\s+(my\s+)?notes", lowered):
        rows = list_notes_db()
        if not rows:
            return "You don't have any saved notes yet. Use: note: Title | note content"
        lines = ["Your saved notes:"]
        for row in rows[:15]:
            content = re.sub(r"\s+", " ", str(row[2] or "")).strip()
            preview = (content[:100] + "…") if len(content) > 100 else content
            lines.append(f"#{row[0]} — {row[1]}" + (f": {preview}" if preview else ""))
        if len(rows) > 15:
            lines.append(f"…and {len(rows) - 15} more. Open Notes & reminders to view all.")
        return "\n".join(lines)

    # List active reminders (one-off and repeating).
    if re.fullmatch(r"(show|list|view|check)\s+(my\s+)?(pending\s+|upcoming\s+|active\s+)?reminders", lowered):
        reminders = with_db(nova_reminders.active)
        if not reminders:
            return "You have no active reminders."
        now = datetime.now()
        lines = ["Your active reminders:"]
        for reminder in reminders[:15]:
            overdue = " (overdue)" if not reminder.recurring and reminder.due <= now else ""
            lines.append(f"#{reminder.id} — {reminder.title}: {reminder.describe(now)}{overdue}")
        if len(reminders) > 15:
            lines.append(f"…and {len(reminders) - 15} more. Open Notes & reminders to see them all.")
        return "\n".join(lines)

    # Save a note: "note: Title | content" or "save note: Title | content".
    note_match = re.match(r"^(?:save\s+)?note\s*:\s*(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
    if note_match:
        payload = note_match.group(1).strip()
        if "|" not in payload:
            return "To save a note, use this format:\nnote: Title | Note content"
        title, content = payload.split("|", 1)
        title, content = title.strip(), content.strip()
        if not title or not content:
            return "A note needs both a title and content. Use: note: Title | Note content"
        note_id = save_note_db(title, content, project_id=project_id)   # in the chat's project
        return f"Saved note #{note_id}: {title}"

    return None


def system_prompt(user_text="", record=False):
    """Nova's rules followed by the memories chosen for this message.

    The rules always come first and memories are appended as a delimited
    data block, so a memory can never replace or precede the rules.
    record=True counts each included memory as used (shown in Memories).
    """
    base = getattr(
        backend,
        "SYSTEM_PROMPT",
        "You are Nova, a helpful local AI assistant. Be clear, accurate, and concise.",
    )
    try:
        block, _included = with_db(
            nova_memories.memory_context, user_text, record=record,
            max_items=getattr(backend, "MAX_MEMORIES_IN_CONTEXT", nova_memories.PROMPT_MAX_MEMORIES),
        )
    except Exception as exc:
        log.warning("Memories skipped for this reply: %s", exc)
        block = ""
    return nova_memories.compose_system_prompt(base, block)


_MODEL_CONTEXT = {}


def model_context_max(model):
    """The largest context window the model supports (asked once per model), or None."""
    if model in _MODEL_CONTEXT:
        return _MODEL_CONTEXT[model]
    try:
        value = nova_context.model_context_length(ollama.show(model))
    except Exception as exc:
        log.info("Couldn't read the context length of %s: %s", model, exc)
        return None     # not cached: Ollama may just not be running yet
    _MODEL_CONTEXT[model] = value
    return value


_MODEL_CAPABILITIES = {}


def model_capabilities(model):
    """What the model supports according to Ollama (e.g. "tools" for native tool calling)."""
    if model in _MODEL_CAPABILITIES:
        return _MODEL_CAPABILITIES[model]
    try:
        info = ollama.show(model)
        value = list(getattr(info, "capabilities", None) or (info.get("capabilities") if isinstance(info, dict)
                                                              else None) or [])
    except Exception as exc:
        log.info("Couldn't read the capabilities of %s: %s", model, exc)
        return []                          # not cached: Ollama may just not be running yet
    _MODEL_CAPABILITIES[model] = value
    return value


def context_budget(model, settings=None):
    """The context budget for a request: the Context size setting, capped by the model."""
    size = settings.get("context_size") if isinstance(settings, dict) else None
    return nova_context.budget_for(size or nova_context.DEFAULT_CONTEXT_SIZE, model_context_max(model))


def build_context(session_id, text, budget, saved=True):
    """Everything relevant to one message, from this PC only (see nova_context)."""
    base = getattr(backend, "SYSTEM_PROMPT", "You are Nova, a helpful local AI assistant.")
    return with_db(nova_context.build, session_id, text, budget, base, documents=nova_documents, saved=saved)


def record_context(app, ctx, message_id):
    """Save what an answer used (for “What Nova used”) and count memory use. Never fatal."""
    try:
        with_db(nova_context.log_context, ctx, message_id)
        if ctx.memory_ids:
            with_db(nova_memories.record_usage, ctx.memory_ids)
    except Exception as exc:
        log.warning("Couldn't record the context of an answer: %s", exc)
        return
    line = ctx.summary_line()
    if line and message_id is not None:
        app.events.put(("context_link", (ctx.session_id, message_id, line)))


def initialize_context_tables():
    """Projects, chat summaries and the context record (additive; backs up before changing tables)."""
    con = get_db_connection()
    try:
        nova_context.ensure_schema(con, getattr(backend, "DB_PATH", None), backup=nova_memories.backup_database)
    finally:
        con.close()


def save_message_safe(session_id, role, content):
    fn = getattr(backend, "save_message", None)
    if not callable(fn):
        raise RuntimeError("assistant.py does not expose save_message().")
    return fn(session_id, role, content)   # the new message's id


def record_and_transcribe(status_callback, stop_event=None):
    """Record until silence, then transcribe locally; no console input.

    Uses the shared voice pipeline in assistant.py, so the CLI and GUI behave
    identically. Raises backend.VoiceInputError with a user-facing message.
    """
    audio = backend.record_until_silence(status_callback, stop_event)
    return backend.transcribe_audio(audio, status_callback, stop_event)



# -------------------- Allowlisted Windows actions --------------------
FILE_SEARCH_TIME_LIMIT = 15  # seconds
FILE_SEARCH_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages"}


# These actions are deliberately limited to opening known apps/locations,
# opening websites, and searching common user folders. No shell command from
# the model is ever executed.
def run_local_action(user_text):
    """Run a supported local action and return a user-facing result, or None."""
    text = user_text.strip()
    low = text.lower().strip().rstrip(".!?")

    websites = {
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "github": "https://github.com",
        "chatgpt": "https://chatgpt.com",
    }
    for name, url in websites.items():
        if re.fullmatch(rf"(?:please )?(?:open|launch|go to|visit) (?:the )?{re.escape(name)}", low):
            webbrowser.open(url)
            return f"Opened {name.title()} in your default browser."

    if re.fullmatch(r"(?:please )?(?:open|launch|start) (?:visual studio code|vs code|vscode)", low):
        code_cmd = shutil.which("code")
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Microsoft VS Code/Code.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft VS Code/Code.exe",
        ]
        executable = Path(code_cmd) if code_cmd else next((p for p in candidates if p.is_file()), None)
        if executable and executable.is_file():
            subprocess.Popen([str(executable)], close_fds=True)
            return "Opened Visual Studio Code."
        return "I couldn't find Visual Studio Code. Install it or add its 'code' command to PATH."

    apps = {
        "notepad": ["notepad.exe"],
        "calculator": ["calc.exe"],
        "paint": ["mspaint.exe"],
        "task manager": ["taskmgr.exe"],
    }
    for name, command in apps.items():
        if re.fullmatch(rf"(?:please )?(?:open|launch|start) (?:the )?{re.escape(name)}", low):
            try:
                subprocess.Popen(command, close_fds=True)
                return f"Opened {name.title()}."
            except Exception as exc:
                return f"Couldn't open {name.title()}: {exc}"

    folder_aliases = {
        "downloads": Path.home() / "Downloads",
        "download folder": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "documents folder": Path.home() / "Documents",
        "desktop": Path.home() / "Desktop",
        "desktop folder": Path.home() / "Desktop",
        "pictures": Path.home() / "Pictures",
        "pictures folder": Path.home() / "Pictures",
    }
    match = re.fullmatch(r"(?:please )?(?:open|show) (?:my )?(?:the )?(.+?)(?: folder)?", low)
    if match:
        target = match.group(1).strip()
        if target in folder_aliases:
            folder = folder_aliases[target]
            if folder.is_dir():
                try:
                    os.startfile(str(folder))
                    return f"Opened your {target} folder."
                except Exception as exc:
                    return f"Couldn't open that folder: {exc}"
            return f"The folder wasn't found: {folder}"

    # Search only within known user folders. No arbitrary path is accepted.
    # Only treat a request as a file search when it explicitly mentions a file or
    # one of the searchable folders; otherwise questions like "find me a pasta
    # recipe" must go to the model.
    verb = r"(?:please )?(?:find|search for|look for|locate)"
    place = r"(?:my |the )?(?P<loc>downloads|documents|desktop)(?: folder)?"
    search_patterns = (
        # "find file(s) [named|called] X [in my downloads]"
        rf"{verb} (?:the |a |my )?files? (?:named |called )?(?P<term>.+?)(?: in {place})?",
        # "find X in my downloads"
        rf"{verb} (?P<term>.+?) in {place}",
        # "search my downloads for X"
        rf"(?:please )?search {place} for (?:files? (?:named |called )?)?(?P<term>.+)",
    )
    search_match = next(
        (m for m in (re.fullmatch(p, low) for p in search_patterns) if m), None
    )
    if search_match:
        term = search_match.group("term").strip().strip('"\'')
        location = (search_match.group("loc") or "documents").lower()
        if not term:
            return "Tell me what filename to search for."
        search_root = {
            "downloads": Path.home() / "Downloads",
            "documents": Path.home() / "Documents",
            "desktop": Path.home() / "Desktop",
        }[location]
        if not search_root.is_dir():
            return f"Couldn't find your {location} folder."
        matches, timed_out = search_filenames(search_root, term)
        note = (
            f"\n(Search stopped after {FILE_SEARCH_TIME_LIMIT} seconds; "
            "some folders weren't checked.)" if timed_out else ""
        )
        if not matches:
            return f"No filenames containing '{term}' were found in {location.title()}.{note}"
        lines = [f"Found {len(matches)} matching file(s) in {location.title()} (showing up to 10):"]
        lines.extend(f"• {p}" for p in matches)
        return "\n".join(lines) + note

    return None



def search_filenames(root, term, seconds=FILE_SEARCH_TIME_LIMIT, should_stop=None, limit=10):
    """File names under root containing term (case-insensitive). Returns (matches, stopped_early).

    Read-only: only directory listings are read. Hidden folders and bulky
    dependency folders are skipped, and unreadable folders are ignored.
    """
    matches, needle = [], str(term).casefold()
    deadline = time.monotonic() + seconds
    for dirpath, dirnames, filenames in safe_walk(root):  # never follows links/junctions out of root
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in FILE_SEARCH_SKIP_DIRS]
        for name in filenames:
            if needle in name.casefold():
                matches.append(Path(dirpath) / name)
                if len(matches) >= limit:
                    return matches, False
        if time.monotonic() > deadline or (should_stop and should_stop()):
            return matches, True
    return matches, False


def find_vscode_exe():
    """Path to Code.exe, or None.

    Never the `code` command on PATH: that is a .cmd batch file, and arguments
    passed to batch files can be re-interpreted by cmd.exe.
    """
    candidates = []
    shim = shutil.which("code")
    if shim:
        candidates.append(Path(shim).resolve().parent.parent / "Code.exe")
    candidates += [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Microsoft VS Code/Code.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft VS Code/Code.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft VS Code/Code.exe",
    ]
    return next((str(p) for p in candidates if p.is_file()), None)


def get_note_db(note_id):
    con = get_db_connection()
    try:
        return con.execute("SELECT id, title, content, created_at, updated_at FROM nova_notes WHERE id = ?",
                           (note_id,)).fetchone()
    finally:
        con.close()


def get_reminder_db(reminder_id):
    con = get_db_connection()
    try:
        return con.execute("SELECT id, title, due_at, completed FROM nova_reminders WHERE id = ?",
                           (reminder_id,)).fetchone()
    finally:
        con.close()


def task_services():
    """The only operations multi-step tasks can perform (see nova_tasks.REGISTRY)."""
    home = Path.home()
    return nova_tasks.Services(
        open_url=webbrowser.open,
        launch=lambda argv: subprocess.Popen(list(argv), close_fds=True, shell=False),
        open_folder=os.startfile,
        find_vscode=find_vscode_exe,
        search_files=search_filenames,
        save_note=save_note_db,
        get_note=get_note_db,
        save_reminder=save_reminder_db,
        get_reminder=get_reminder_db,
        # Where Windows really keeps them (e.g. a Desktop moved into OneDrive).
        known_folders={name: nova_actions.known_folder(name) for name in nova_tasks.KNOWN_FOLDERS},
        home=home,
        # Step 25 (approved folders and document reading are connected to the window's settings in NovaGUI)
        approved_folders=lambda: [],
        protected=lambda: nova_actions.default_protected(Path(__file__).resolve().parent),
        open_file=os.startfile,
        switch_window=nova_actions.switch_window,
        recycle=nova_actions.recycle,
        clipboard_get=nova_actions.clipboard_get,
        clipboard_set=nova_actions.clipboard_set,
        index_document=lambda path, should_stop: index_document_now(path, should_stop),
    )


def index_document_now(path, should_stop=None, project_id=None):
    """Read one document into the index right away (the read_document task step).

    Uses the same isolated reader process as the Documents window. Returns
    (status, passages, detail); the text itself stays in the document index.
    """
    con = get_db_connection()
    worker = nova_documents.ExtractionWorker()
    try:
        source_id, _created = nova_documents.add_source(con, path, project_id)
        nova_documents.index_source(con, source_id, should_stop=should_stop, worker=worker)
        row = con.execute("SELECT status, chunk_count, status_detail FROM doc_files WHERE path = ?",
                          (str(nova_documents.normalize_path(path)),)).fetchone()
        return (row[0], row[1], row[2]) if row else ("error", 0, "it wasn't found after reading")
    finally:
        worker.close()
        con.close()


def initialize_task_tables():
    con = get_db_connection()
    try:
        nova_tasks.initialize_schema(con)
    finally:
        con.close()


# -------------------- Offline wake-word detection --------------------
def find_vosk_model():
    configured = os.environ.get("VOSK_MODEL_PATH")
    root = Path(__file__).resolve().parent
    home = Path.home()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [
        root / "vosk-model-small-en-us-0.15",
        root / "models" / "vosk-model-small-en-us-0.15",
        home / "vosk-model-small-en-us-0.15",
        home / "Models" / "vosk-model-small-en-us-0.15",
    ]
    for parent in (root, root / "models", home):
        try:
            if parent.is_dir():
                candidates.extend(p for p in parent.glob("vosk-model*") if p.is_dir())
        except OSError:
            pass
    seen = set()
    for path in candidates:
        try:
            path = path.resolve()
            if path not in seen:
                seen.add(path)
                if path.is_dir() and (path / "am").is_dir() and (path / "conf").is_dir():
                    return path
        except OSError:
            pass
    return None


def make_voice_loop(settings, handlers, stop_event, paused, status=lambda text: None):
    """The hands-free listener: the local Vosk model for the wake phrase, assistant.py to record and transcribe."""
    try:
        from vosk import KaldiRecognizer
    except Exception as exc:
        raise RuntimeError(
            "Wake-word mode needs Vosk. Install it in your Nova virtual environment: pip install vosk"
        ) from exc
    model_path = find_vosk_model()
    if model_path is None:
        raise FileNotFoundError(
            "No local Vosk model was found. Extract the Vosk small English model into "
            "the NovaAI folder as 'vosk-model-small-en-us-0.15', or set VOSK_MODEL_PATH "
            "to the extracted model directory."
        )
    if str(model_path) not in _VOSK_MODELS:
        status("Loading local wake-word model…")
    model = load_vosk_model(model_path)
    config = nova_voice.VoiceConfig.from_settings(settings)
    try:
        phrase = nova_voice.WakePhrase(config.phrase, nova_voice.vosk_word_checker(model))
    except ValueError as exc:
        raise RuntimeError(f"{exc} Change it in Settings › Voice & microphone.") from None
    return nova_voice.VoiceLoop(
        VOICE_STATE, phrase, config, handlers,
        open_source=lambda: nova_voice.microphone_source(getattr(backend, "MICROPHONE_DEVICE", 1)),
        make_recognizer=lambda grammar: KaldiRecognizer(model, 16000, grammar),
        record=backend.record_until_silence, transcribe=backend.transcribe_audio, beep=nova_voice.wake_beep,
        base_gate=getattr(backend, "SPEECH_THRESHOLD", 450), stop_event=stop_event, paused=paused)


class GuiVoiceHandlers(nova_voice.VoiceHandlers):
    """The hands-free loop's messages, passed to the GUI thread through its event queue."""

    def __init__(self, app):
        self.app = app

    def status(self, text):
        self.app.events.put(("status", text))

    def command(self, text):
        self.app._voice_handoff = True
        self.app.events.put(("wake_transcript_ready", text))

    def control(self, kind):
        self.app.events.put(("voice_control", kind))

    def busy(self, text):
        self.app.events.put(("system", f"Heard “{text}”, but Nova is busy, so it wasn't sent. Say the wake "
                                       "phrase and then “cancel” to stop the current task."))

    def approval_answer(self, approval, value):
        self.app.events.put(("voice_approval", (approval, value)))

    def warning(self, text):
        self.app.events.put(("system", text))

    def stop_speaking(self):
        SPEAKER.stop()

    def is_busy(self):
        return bool(self.app.busy)



def nova_icon_image(size=64):
    """Nova's app/tray icon as a PIL image, or None if Pillow isn't installed."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2 * scale, 2 * scale, 62 * scale, 62 * scale), radius=16 * scale, fill=(124, 140, 255, 255))
    # Four-point sparkle
    c, r, w = 32 * scale, 20 * scale, 5 * scale
    draw.polygon([(c, c - r), (c + w, c - w), (c + r, c), (c + w, c + w),
                  (c, c + r), (c - w, c + w), (c - r, c), (c - w, c - w)], fill=(255, 255, 255, 255))
    return image


class NovaGUI:
    SUGGESTIONS = (
        "Remind me to stretch in 30 minutes",
        "Open YouTube",
        "Find file invoice in downloads",
        "Explain how a CPU works",
    )

    def __init__(self, root):
        self.root = root
        self.settings, settings_warnings = load_nova_settings()
        startup_notes = list(settings_warnings) + apply_runtime_settings(self.settings)
        self._settings_warnings_at_start = list(settings_warnings)
        self.tray_dependencies = load_tray_dependencies()
        self.tray_icon = None
        self.tray_thread = None
        self._exiting = False
        self.fonts = Fonts(root)
        self.root.title(APP_TITLE)
        self.root.geometry("1180x780")
        self.root.minsize(900, 600)
        if isinstance(self.root, ctk.CTk):
            self.root.configure(fg_color=BG)
        else:
            self.root.configure(bg=BG)
        self._icon_path = self._make_icon_file()
        self._apply_icon(self.root)
        self.events = queue.Queue()
        self.busy = False
        self.voice_response = False
        self.wake_active = False
        self.wake_stop_event = threading.Event()
        self.wake_resume_event = threading.Event()
        self.wake_thread = None
        self.voice_loop = None             # hands-free listener (nova_voice.VoiceLoop) while it runs
        self.voice_paused = threading.Event()
        self._voice_handoff = False        # a spoken request is on its way to the GUI thread
        self._agent_voice = False          # the running agent task was asked for by voice
        self._voice_approvals = {}         # pending spoken approval -> the agent window's decide()
        VOICE_STATE.on_change = lambda old, new: self.events.put(("voice_state", new))
        SPEAKER.is_busy = lambda: self.busy
        self.voice_thread = None
        self.voice_stop_event = threading.Event()
        self._jobs = {}            # pending Tk timers by name (one each; cancelled at shutdown)
        self._maintenance = False  # set during a restore: pauses reminder polling
        self._backup_running = False
        self._settings_warnings = []
        self._wake_autostart = False
        self._cancel_reply = threading.Event()
        self._stop_mode = False
        self._reply_started = False
        self._reply_began = 0.0
        self._slow_hint_shown = False
        self._model_used_at = -1e9
        self._warming = False
        self._chat_limit, self._chat_latest, self._chat_first_id = CHAT_PAGE, True, None
        self._loading_chat = False
        self.doc_status = ""
        self._documents_ui = None
        self.indexer = nova_documents.DocumentIndexer(
            get_db_connection,
            on_progress=lambda text: self.events.put(("doc_progress", text)),
            on_changed=lambda: self.events.put(("doc_changed", None)),
        ).start()
        self._task_ui = None
        self._pending_reminder = None   # a reminder question awaiting the user's answer
        self._reminders_ui = None
        self._alert_windows = []
        self.task_runner = nova_tasks.TaskRunner(
            get_db_connection, task_services(),
            on_update=lambda *update: self.events.put(("task_update", update)),
        )
        # Notes a task saves belong to the project of the chat it runs from.
        self.task_runner.services.save_note = lambda title, content: save_note_db(
            title, content, project_id=self._chat_project_id())
        # Step 25: the folders you allowed (Tasks › Allowed folders); documents read by a task
        # join the open chat's project and show up in Documents.
        self.task_runner.services.approved_folders = lambda: list(self.settings.get("approved_folders") or [])
        self.task_runner.services.index_document = self._index_document_for_task
        self._pending_link = None       # (session_id, message_id, text) shown under the next finished answer
        self._projects_ui = None
        self._project_menu_labels = {}
        self.secrets = nova_online.CredentialStore()    # API keys for optional online providers
        self.agent_registry = nova_agent.default_registry()   # Step 28: tools the agent may use
        self._agent_cancel = None
        self._agent_ui = None
        self._http = None                               # created on the first online request
        self._summary_cancel = threading.Event()
        self._summarizing = False
        try:
            initialize_context_tables()
        except sqlite3.Error as exc:
            log.error("Couldn't prepare projects and chat summaries: %s", exc)
        self.project_view = self._saved_project_view()
        self.session_id = self._latest_session_in_view() or current_session_id()
        self.sessions = []
        self.chat_cache = []
        self._build_ui()
        self.refresh_sessions(select_current=True)
        self.load_chat()
        initialize_productivity_tables()
        self.set_status("Ready")
        self._settings_warnings = self._settings_warnings_at_start
        self._schedule("events", 100, self.poll_events)
        self._schedule("reminders", 1000, self.poll_due_reminders)
        self._schedule("auto_backup", 15000, self._auto_backup)
        self._schedule("prewarm", 1500, self._prewarm_imports)
        if self.settings.get("model_warmup") == "startup":
            self._schedule("warmup", 3000, self._maybe_warm_model)
        nova_perf.LOG_TIMINGS = bool(self.settings.get("log_performance"))
        self.root.after_idle(lambda: nova_perf.record("startup: until the window is ready",
                                                      (time.perf_counter() - _PROCESS_STARTED) * 1000))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.tray_dependencies:
            self.start_tray_icon()
        for note in startup_notes:
            self.append_message("system", note)
        if self.settings.get("wake_word_enabled"):
            self.root.after(600, self._autostart_wake_word)
        threading.Thread(target=self._check_saved_model, daemon=True, name="NovaModelCheck").start()
        self.root.after(4000, self._startup_document_refresh)
        self.root.after(200, self.input_text.focus_set)

    def _startup_document_refresh(self):
        """Re-check indexed documents for changes in the background after launch."""
        try:
            con = get_db_connection()
            try:
                has_sources = con.execute("SELECT 1 FROM doc_sources LIMIT 1").fetchone() is not None
            finally:
                con.close()
        except sqlite3.Error as exc:
            log.warning("Document refresh skipped: %s", exc)
            return
        if has_sources:
            self.indexer.refresh()

    def _check_saved_model(self):
        """At startup, warn (once, in the chat) if the configured model isn't installed."""
        model = active_model()
        try:
            installed = nova_settings.list_ollama_models(timeout=5)
        except nova_settings.OllamaUnavailable as exc:
            log.warning("Startup model check skipped: %s", exc)
            return  # the chat itself reports this clearly on first use
        if model not in installed and f"{model}:latest" not in installed:
            log.warning("Configured model %s is not installed (installed: %s)", model, installed)
            choices = ", ".join(installed) if installed else "none"
            self.events.put((
                "system",
                f"The model “{model}” isn't installed in Ollama. Run “ollama pull {model}”, "
                f"or choose an installed model in Settings › Model (installed: {choices}).",
            ))

    def _persist_setting(self, key, value):
        """Save one setting immediately (used by controls outside the Settings window)."""
        if self.settings.get(key) == value:
            return
        self.settings[key] = value
        try:
            self.settings = save_nova_settings(self.settings)
        except OSError as exc:
            log.error("Could not save setting %s: %s", key, exc)
            self.set_status("Couldn't save settings")

    # -------------------- window icon --------------------
    def _make_icon_file(self):
        image = nova_icon_image(64)
        if image is None:
            return None
        try:
            path = Path(tempfile.gettempdir()) / "nova_icon.ico"
            image.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
            return path
        except Exception:
            return None

    def _apply_icon(self, window):
        """Set Nova's icon. CustomTkinter swaps in its own icon ~200 ms after a
        window opens unless iconbitmap() was called, so set it now and again later."""
        if not self._icon_path:
            return

        def apply():
            try:
                window.iconbitmap(str(self._icon_path))
            except tk.TclError:
                pass

        apply()
        window.after(250, apply)

    # -------------------- layout --------------------
    def _build_ui(self):
        f = self.fonts
        self.status_var = tk.StringVar(value="Starting…")

        shell = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        shell.pack(fill="both", expand=True)

        # Sidebar
        sidebar = ctk.CTkFrame(shell, fg_color=SIDEBAR, corner_radius=0, width=272)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(20, 16))
        ctk.CTkLabel(
            brand, text="✦", width=32, height=32, corner_radius=10, fg_color=ACCENT,
            text_color="#ffffff", font=ctk.CTkFont(family=f.symbol, size=16),
        ).pack(side="left")
        ctk.CTkLabel(brand, text="Nova", font=f.heading(21), text_color=TEXT).pack(side="left", padx=(10, 0))

        NavButton(sidebar, f, Icon.ADD, "New chat", self.new_chat, filled=True).pack(fill="x", padx=12, pady=(0, 6))
        search_nav = NavButton(sidebar, f, Icon.SEARCH, "Search chats", self.open_search)
        search_nav.pack(fill="x", padx=12, pady=(0, 6))
        Tooltip(search_nav, "Search all conversations (Ctrl+F)", f)
        self.root.bind_all("<Control-f>", lambda _e: (self.open_search(), "break")[1])
        self.root.bind_all("<Control-F>", lambda _e: (self.open_search(), "break")[1])
        # Project: which chats are listed and where new chats start (and what context Nova uses).
        self.project_var = tk.StringVar(value="All chats")
        self.project_menu = ctk.CTkOptionMenu(
            sidebar, variable=self.project_var, values=["All chats"], height=34, corner_radius=10,
            dynamic_resizing=False, font=f.ui(13), dropdown_font=f.ui(13), fg_color=SURFACE, button_color=SURFACE,
            button_hover_color=SURFACE_2, text_color=TEXT, dropdown_fg_color=SURFACE,
            dropdown_hover_color=SURFACE_2, dropdown_text_color=TEXT, command=self._on_project_menu,
        )
        self.project_menu.pack(fill="x", padx=12, pady=(2, 6))
        Tooltip(self.project_menu, "Project: which chats are listed, where new chats start, and which chats, "
                                   "notes and documents Nova uses", f)
        self._refresh_project_menu()
        self.session_list = SessionList(sidebar, f, on_select=self.select_session, on_delete=self.delete_chat)
        self.session_list.on_menu = self._session_menu
        self.session_list.pack(fill="both", expand=True, padx=(4, 0))

        ctk.CTkFrame(sidebar, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x", padx=16, pady=8)
        NavButton(sidebar, f, Icon.DOCUMENT, "Documents", self.open_documents).pack(fill="x", padx=12, pady=1)
        NavButton(sidebar, f, Icon.TASKS, "Tasks", self.open_tasks).pack(fill="x", padx=12, pady=1)
        NavButton(sidebar, f, Icon.MEMORY, "Memories", self.open_memories).pack(fill="x", padx=12, pady=1)
        NavButton(sidebar, f, Icon.NOTE, "Notes & reminders", self.open_productivity).pack(fill="x", padx=12, pady=1)
        NavButton(sidebar, f, Icon.SETTINGS, "Settings", self.open_startup_settings).pack(fill="x", padx=12, pady=1)
        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(10, 16))
        ctk.CTkLabel(footer, text="●", font=ctk.CTkFont(family=f.symbol, size=9), text_color=SUCCESS, width=10).pack(side="left")
        self.model_label = ctk.CTkLabel(footer, text="", font=f.ui(12), text_color=SUBTLE)
        self.model_label.pack(side="left", padx=(6, 0))
        self._update_model_label()

        # Main column
        main = ctk.CTkFrame(shell, fg_color=BG, corner_radius=0)
        main.pack(side="left", fill="both", expand=True)

        header = ctk.CTkFrame(main, fg_color=BG, corner_radius=0, height=62)
        header.pack(fill="x")
        header.pack_propagate(False)
        self._full_title = "New conversation"
        self._title_font = f.ui(16, semibold=True)
        self.status_pill = StatusPill(header, f, self.status_var)
        self.status_pill.pack(side="right", padx=22)
        self.title_label = ctk.CTkLabel(header, text=self._full_title, font=self._title_font, text_color=TEXT, anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True, padx=(28, 12))
        header.bind("<Configure>", lambda _e: self._fit_title(), add="+")
        ctk.CTkFrame(main, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x")

        # Composer is packed before the chat view so it can never be pushed off-screen.
        bottom = ctk.CTkFrame(main, fg_color=BG, corner_radius=0)
        bottom.pack(side="bottom", fill="x")
        self.composer_wrap = ctk.CTkFrame(bottom, fg_color="transparent")
        self.composer_wrap.pack(fill="x", padx=40, pady=(4, 16))

        composer = ctk.CTkFrame(self.composer_wrap, fg_color=SURFACE, corner_radius=24, border_width=1, border_color=BORDER)
        composer.pack(fill="x")
        composer.grid_columnconfigure(0, weight=1)
        self._input_font = f.ui(15)
        self._line_height = self._input_font.metrics("linespace")
        self.input_text = ctk.CTkTextbox(
            composer, fg_color=SURFACE, text_color=TEXT, font=self._input_font,
            height=self._line_height + 10, wrap="word", border_width=0,
            corner_radius=0, activate_scrollbars=False,
        )
        self.input_text.grid(row=0, column=0, sticky="ew", padx=(18, 6), pady=11)
        self.placeholder = ctk.CTkLabel(
            composer, text="Message Nova…", font=self._input_font,
            text_color=SUBTLE, fg_color=SURFACE, height=self._line_height,
        )
        self.placeholder.bind("<Button-1>", lambda _e: self.input_text.focus_set())
        set_cursor(self.placeholder, "xterm")

        buttons = ctk.CTkFrame(composer, fg_color="transparent")
        buttons.grid(row=0, column=1, sticky="se", padx=(0, 9), pady=8)
        self.voice_button = icon_button(buttons, f, Icon.MIC, self.start_voice, size=38, icon_size=16, text_color_disabled=BORDER)
        self.voice_button.pack(side="left", padx=(0, 6))
        Tooltip(self.voice_button, "Speak to Nova", f)
        self.send_button = icon_button(
            buttons, f, Icon.SEND, self.send_text, size=38, icon_size=15,
            fg=ACCENT, hover=ACCENT_HOVER, text_color="#ffffff", text_color_disabled=SUBTLE,
        )
        self.send_button.pack(side="left")
        self._send_tooltip = Tooltip(self.send_button, "Send (Enter)", f)

        hints = ctk.CTkFrame(self.composer_wrap, fg_color="transparent")
        hints.pack(fill="x", pady=(9, 0), padx=8)
        self.wake_switch = ctk.CTkSwitch(
            hints, text="Hey Nova", font=f.ui(13), text_color=MUTED,
            command=self.toggle_wake_word, progress_color=ACCENT, fg_color=SURFACE_2,
            button_color=TEXT, button_hover_color="#ffffff", switch_width=34, switch_height=18,
        )
        self.wake_switch.pack(side="left")
        Tooltip(self.wake_switch, "Hands-free: listen for the wake phrase — works offline", f)
        self.voice_state_button = ctk.CTkButton(
            hints, text="", font=f.ui(12), height=22, width=10, corner_radius=8, fg_color="transparent",
            hover_color=SURFACE_2, text_color=MUTED, command=self.toggle_voice_pause)
        Tooltip(self.voice_state_button, "Click to pause or resume listening (the microphone closes while paused)", f)
        self.stop_speaking_button = ctk.CTkButton(
            hints, text=f"{Icon.STOP}  Stop speaking", font=f.ui(12), height=22, width=10, corner_radius=8,
            fg_color=DANGER_SOFT, hover_color="#4a272c", text_color=DANGER, command=self.stop_speaking)
        Tooltip(self.stop_speaking_button, "Stop speaking (Esc)", f)
        ctk.CTkLabel(hints, text="Enter to send  ·  Shift+Enter for a new line", font=f.ui(12), text_color=SUBTLE).pack(side="right")

        self.chat = ChatView(main, f, on_suggestion=self._use_suggestion)
        self.chat.pack(fill="both", expand=True)
        self.chat.bind("<Configure>", self._align_composer, add="+")

        self.input_text.bind("<Return>", self._enter_behavior)
        self.input_text.bind("<Control-Return>", self._send_shortcut)
        for sequence in ("<KeyRelease>", "<FocusIn>", "<FocusOut>", "<<Paste>>"):
            self.input_text.bind(sequence, lambda _e: self.root.after_idle(self._update_composer))
        self.input_text.bind("<KeyRelease>", self._on_typing, add="+")
        self.root.bind("<Escape>", self._on_escape)
        self.root.after(50, self._update_composer)

    def _update_model_label(self):
        self.model_label.configure(text=fit_text(f"Running locally · {active_model()}", self.fonts.ui(12), 210))

    def _align_composer(self, _event=None):
        """Keep the composer lined up with the chat's reading column."""
        width = self.chat.winfo_width()
        column = self.chat.column_width()
        left = max(20, (width - 14 - column) // 2)
        right = max(20, width - left - column)
        self.composer_wrap.pack_configure(padx=(left, right))

    def _update_composer(self):
        """Grow the input box with its content (1–8 lines) and toggle the placeholder."""
        try:
            text = self.input_text.get("1.0", "end-1c")
            inner = self.input_text._textbox
            lines = inner.count("1.0", "end", "displaylines")
            lines = lines[0] if isinstance(lines, tuple) else (lines or 1)
        except tk.TclError:
            return
        lines = max(1, min(8, int(lines)))
        self.input_text.configure(height=lines * self._line_height + 10)
        if text:
            self.placeholder.place_forget()
        else:
            self.placeholder.place(in_=self.input_text, x=6, y=4)

    def _use_suggestion(self, suggestion):
        if self.busy:
            return
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", suggestion)
        self.input_text.focus_set()
        self._update_composer()

    def set_status(self, value):
        self.status_var.set(value)
        lower = str(value).lower()
        if "fail" in lower or "error" in lower:
            color = DANGER
        elif self.busy or any(word in lower for word in (
            "listening", "transcrib", "thinking", "working", "starting", "stopping", "speak", "loading",
        )):
            color = WARNING
        elif self.wake_active:
            color = ACCENT
        else:
            color = SUCCESS
        self.status_pill.set_color(color)

    # -------------------- conversation view --------------------
    def append_message(self, role, content):
        if role == "user" and not self._loading_chat and not self._chat_latest:
            self.load_chat()   # new messages always go below the latest ones
        if role == "user":
            self.chat.add_user(content)
        elif role == "assistant":
            self.chat.add_assistant(content)
        else:
            self.chat.add_info(content)

    def clear_chat_view(self):
        self.chat.clear()

    def load_chat(self, anchor_id=None, limit=None):
        """Draw a conversation: the latest CHAT_PAGE messages (older ones on request),
        or the messages around anchor_id (a search result)."""
        started = time.perf_counter()
        limit = limit or CHAT_PAGE
        rows, earlier, later = fetch_message_window(self.session_id, limit, anchor_id)
        self._chat_limit, self._chat_latest = limit, later == 0
        self._chat_first_id = rows[0][0] if rows else None
        if not rows:
            self.chat.show_welcome(self.SUGGESTIONS)
        else:
            self._loading_chat = True
            self.chat.clear()
            self.chat.begin_bulk()
            try:
                if earlier:
                    self.chat.add_action(f"Show earlier messages ({earlier} more)", self._show_earlier)
                try:
                    links = with_db(nova_context.summaries_for_messages,
                                    [mid for mid, role, _c in rows if role == "assistant"])
                except sqlite3.Error:
                    links = {}
                for message_id, role, content in rows:
                    self.chat.mark_message(f"msg_{message_id}")  # lets search results jump here
                    self.append_message(role, content)
                    if message_id in links:
                        self._add_context_link(message_id, links[message_id])
                if later:
                    self.chat.add_action(f"Jump to the latest messages ({later} newer)", lambda: self.load_chat())
            finally:
                self.chat.end_bulk()
                self._loading_chat = False
            self.root.update_idletasks()
            if anchor_id is None:
                self.chat.scroll_to_end()
        self._update_title()
        nova_perf.record("open a chat (draw messages)", (time.perf_counter() - started) * 1000)

    def _update_title(self):
        title = next((label for sid, label, _date in self.sessions if sid == self.session_id), None)
        if title is None and self.session_id is not None:
            try:
                row = with_db(lambda con: con.execute("SELECT title FROM sessions WHERE id = ?",
                                                      (self.session_id,)).fetchone())
                title = row[0] if row else None
            except sqlite3.Error:
                title = None
        project = self._chat_project()
        self._full_title = (f"{project.name}  ›  " if project else "") + (title or "New conversation")
        self._fit_title()

    def _fit_title(self):
        """Fit the conversation title into the header space left of the status pill."""
        header = self.title_label.master
        available = header.winfo_width() - self.status_pill.winfo_width() - 28 - 12 - 44
        if available < 100:
            available = 600
        self.title_label.configure(text=fit_text(self._full_title, self._title_font, available))

    def refresh_sessions(self, select_current=False):
        self.sessions = fetch_sessions(self.project_view)
        self.session_list.set_sessions(self.sessions, self.session_id)
        self._update_title()

    def select_session(self, session_id):
        if session_id == self.session_id:
            return
        if self.busy:
            # The running reply belongs to the open conversation; stay on it.
            self.set_status("Wait for Nova to finish before switching conversations")
            return
        self.session_id = session_id
        self.session_list.set_selected(session_id)
        self.load_chat()

    def new_chat(self):
        if self.busy:
            self.set_status("Wait for Nova to finish before starting a new chat")
            return
        # Reuse the open chat if it's still empty instead of piling up blank ones.
        if self.session_id is not None and not has_messages(self.session_id):
            self.load_chat()
            self.input_text.focus_set()
            return
        self.session_id = new_session_id()
        if self.session_id is None:
            messagebox.showerror("Nova", "Could not create a new conversation.", parent=self.root)
            return
        self._assign_view_project(self.session_id)
        self.refresh_sessions(select_current=True)
        self.load_chat()
        self.set_status("Ready")
        self.input_text.focus_set()

    def delete_chat(self, session_id=None):
        """Permanently delete a conversation (the open one by default)."""
        if self.busy:
            self.set_status("Wait for Nova to finish before deleting a chat")
            return
        if session_id is None:
            session_id = self.session_id
        if session_id is None:
            return

        title = next(
            (label for sid, label, _date in self.sessions if sid == session_id),
            f"Chat {session_id}",
        )
        count = count_messages(session_id)
        if not messagebox.askyesno(
            "Delete chat",
            f"Delete “{title}”?\n\n"
            f"Its {count} message{'s' if count != 1 else ''} will be permanently "
            "deleted. This can't be undone.",
            icon="warning",
            parent=self.root,
        ):
            return

        try:
            backend.delete_session(session_id)
        except Exception as exc:
            messagebox.showerror("Delete chat", f"Couldn't delete the chat: {exc}", parent=self.root)
            return

        if session_id == self.session_id:
            # Open the most recent remaining chat, or start a fresh one.
            remaining = fetch_sessions()
            self.session_id = remaining[0][0] if remaining else new_session_id()
            self.load_chat()
        self.refresh_sessions(select_current=True)
        self.set_status("Chat deleted")

    def _enter_behavior(self, event):
        if event.state & 0x1:  # Shift+Enter inserts newline
            self.root.after_idle(self._update_composer)
            return None
        self.send_text()
        return "break"

    def _send_shortcut(self, _event):
        self.send_text()
        return "break"

    # -------------------- requests --------------------
    def handle_user_message(self, text, speak=False):
        """Handle local productivity and allowlisted Windows actions before model chat.

        Runs on a worker thread because some actions (file search) can take a
        while; the result comes back through the event queue.
        """
        if self.busy:
            return
        self.busy = True
        self.set_controls_enabled(False)
        self.set_status("Working…")
        threading.Thread(
            target=self._action_worker,
            args=(self.session_id, text, speak),
            daemon=True,
        ).start()

    def _action_worker(self, session_id, text, speak):
        pending, self._pending_reminder = getattr(self, "_pending_reminder", None), None
        try:
            handled = answer_pending_reminder(text, pending)
        except Exception as exc:
            log.error("Reminder follow-up failed: %s", exc)
            handled = (f"I couldn't set that reminder: {exc}", None)
        if handled is not None:
            self._finish_reminder_reply(session_id, text, handled, speak)
            return
        if nova_context.is_inspect_command(text):
            self.events.put(("context_inspect", (None, session_id, True)))
            return
        project_name = nova_context.parse_project_command(text)
        if project_name is not None:
            self.events.put(("project_command", (project_name, speak)))
            return
        agent_request = nova_agent.parse_command(text)   # "agent: …" (Step 28)
        if agent_request is not None:
            self._run_agent(session_id, text, agent_request, speak)
            return
        online = nova_online.parse_command(text)     # only explicit requests ever go online
        if online is not None:
            self._online_request(session_id, text, *online, speak)
            return
        question = nova_documents.parse_document_question(text)
        if question is not None:
            self._answer_from_documents(session_id, text, question, speak)
            return
        # Memory changes from chat always go through a confirmation dialog.
        remember = nova_memories.parse_remember(text)
        if remember is not None:
            self.events.put(("memory_remember", (session_id, text, remember, speak)))
            return
        forget = nova_memories.parse_forget(text)
        if forget is not None:
            self.events.put(("memory_forget", (session_id, text, forget, speak)))
            return
        # Multi-step tasks: "task: …", or 2+ steps Nova's planner fully understands.
        # Single commands keep their existing handling below.
        if nova_tasks.explicit_request(text) is not None:
            self.events.put(("status", "Planning task…"))
        try:
            plan = nova_tasks.plan_request(text, self.task_runner.services, chat=ollama.chat, model=active_model())
        except Exception as exc:
            log.error("Task planning failed: %s", exc)
            plan = None
        if plan is not None:
            self.events.put(("task_plan", (session_id, text, plan, speak)))
            return
        if self.settings.get("memory_suggestions_enabled"):
            try:  # only the user's own words; nothing is saved until approved
                created = with_db(nova_memories.suggest_from_message, text, session_id)
                if created:
                    self.events.put(("memory_suggested", len(created)))
            except Exception as exc:
                log.warning("Memory suggestions skipped: %s", exc)
        try:
            handled = new_reminder_request(text)
        except Exception as exc:
            log.error("Reminder request failed: %s", exc)
            handled = (f"I couldn't set that reminder: {exc}", None)
        if handled is not None:
            self._finish_reminder_reply(session_id, text, handled, speak)
            return
        try:
            try:
                action_result = handle_productivity_command(text, with_db(nova_context.session_project, session_id))
            except Exception as exc:
                action_result = f"Couldn't complete that local productivity command: {exc}"
            if action_result is None:
                action_result = run_local_action(text)
        except Exception as exc:
            action_result = f"Couldn't complete that action: {exc}"
        if action_result is None:
            self.events.put(("action_none", (text, speak)))
            return
        try:
            save_message_safe(session_id, "user", text)
            save_message_safe(session_id, "assistant", action_result)
        except Exception as exc:
            action_result += f"\n\n(Note: couldn't save this exchange to chat history: {exc})"
        self.events.put(("action_done", (action_result, speak)))

    def finish_action(self, action_result, speak):
        """Main-thread completion of a local action started by handle_user_message."""
        self.busy = False
        self.set_controls_enabled(True)
        self.append_message("assistant", action_result)
        self.set_status("Ready")
        self.refresh_sessions(select_current=True)
        spoken = speak if isinstance(speak, str) else action_result   # e.g. an agent's short spoken summary
        if speak:
            def speak_worker():
                try:
                    speak_text(spoken)
                except Exception as exc:
                    log.warning("Speech output failed: %s", exc)
                finally:
                    self._voice_done()
            threading.Thread(target=speak_worker, daemon=True).start()
        else:
            self._voice_done()

    def send_text(self):
        if self.busy:
            return
        text = self.input_text.get("1.0", "end").strip()
        if not text:
            return
        self.input_text.delete("1.0", "end")
        self._update_composer()
        self.append_message("user", text)
        self.handle_user_message(text, speak=False)

    # -------------------- voice --------------------
    def _sync_wake_switch(self):
        if self.wake_active:
            self.wake_switch.select()
        else:
            self.wake_switch.deselect()
        self._refresh_tray_menu()

    def _autostart_wake_word(self):
        """Start "Hey Nova" at launch when the saved preference says so."""
        if self.wake_active or self._exiting:
            return
        reason = wake_word_support()
        if reason:
            log.warning("Wake word enabled in settings but unavailable: %s", reason)
            self.append_message("system", f"“Hey Nova” is enabled in Settings but can't start. {reason}")
            return
        if self.busy:
            self.root.after(1000, self._autostart_wake_word)
            return
        self._wake_autostart = True
        self.toggle_wake_word(persist=False)

    def _restart_wake_listener(self, attempts=40):
        """Restart the wake listener (e.g. after a microphone change) once it has stopped."""
        if self.wake_active:
            self.toggle_wake_word(persist=False)
        if self.wake_listener_running() and attempts > 0:
            self.root.after(250, lambda: self._restart_wake_listener(attempts - 1))
            return
        if self.wake_listener_running():
            self.set_status("Microphone change applies when Hey Nova restarts")
            return
        if not self.wake_active and self.settings.get("wake_word_enabled"):
            self.toggle_wake_word(persist=False)

    def toggle_wake_word(self, persist=True):
        if self.wake_active:
            self.wake_active = False
            self.wake_stop_event.set()
            self.wake_resume_event.set()
            self._sync_wake_switch()
            self._show_voice_state()
            self.set_controls_enabled(not self.busy)
            self.set_status("Stopping wake-word listener…")
            if persist:
                self._persist_setting("wake_word_enabled", False)
            return
        if self.busy:
            self._sync_wake_switch()
            messagebox.showinfo("Hey Nova", "Wait for the current task to finish before enabling wake-word mode.", parent=self.root)
            return
        if persist:
            self._wake_autostart = False  # user-initiated: report errors in a dialog
            self._persist_setting("wake_word_enabled", True)
        self.wake_active = True
        self.voice_paused.clear()
        self._sync_wake_switch()
        self.set_controls_enabled(True)
        self.set_status("Starting wake-word listener…")
        self.events.put(("system", f"Hands-free is on. Say “{phrase_label(self.settings)}”, then your request. "
                                   "Wake-phrase listening happens on this PC, and no audio is saved."))
        self._show_voice_state()
        self._start_wake_thread()

    def _wake_worker(self):
        try:
            loop = make_voice_loop(dict(self.settings), GuiVoiceHandlers(self), self.wake_stop_event,
                                   self.voice_paused, lambda status: self.events.put(("status", status)))
            self.voice_loop = loop
            self.events.put(("status", f"Hands-free on • say “{loop.phrase.label}”"))
            self.events.put(("voice_state", VOICE_STATE.state))
            loop.run()
        except Exception as exc:
            self.events.put(("wake_setup_error", str(exc)))
        finally:
            self.voice_loop = None

    # ---- hands-free voice (Step 29) ----
    def _voice_command(self, text):
        """Main thread: a spoken request from the hands-free loop."""
        self._voice_handoff = False
        if self.busy:
            self.append_message(
                "system",
                f"Heard “{text}”, but Nova was busy, so it wasn't sent. "
                "Say the wake phrase again when the current task finishes.",
            )
            self._voice_done()
            return
        if self.settings.get("voice_route", "agent") == "chat":
            # Like typing it: confirmed first unless “Confirm what I said” is off in Settings.
            self.set_status("Wake phrase heard • confirm command")
            if not self._transcript_confirmed(text):
                self.set_status("Command cancelled • still listening")
                self._voice_done()
                return
            VOICE_STATE.to(nova_voice.PROCESSING)
            self.append_message("user", text)
            self.handle_user_message(text, speak=True)
            return
        # Nova's simple built-in commands run directly; anything else goes to the agent,
        # which plans it and asks (by voice and on screen) before changing anything.
        VOICE_STATE.to(nova_voice.PROCESSING)
        self.append_message("user", text)
        self.busy = True
        self.set_controls_enabled(False)
        self.set_status("Working on your spoken request…")
        threading.Thread(target=self._voice_route, args=(self.session_id, text), daemon=True,
                         name="NovaVoiceRequest").start()

    def _voice_route(self, session_id, text):
        """Worker thread: a spoken request. Built-in commands that only add a reminder, answer the
        time, list things or open an allowlisted app, site or folder run at once, exactly as if
        typed (none changes or removes files). Everything else goes to the agent."""
        pending, self._pending_reminder = getattr(self, "_pending_reminder", None), None
        try:
            handled = answer_pending_reminder(text, pending)    # e.g. “at 5 pm” after “When?”
            if handled is None:
                handled = new_reminder_request(text)
        except Exception as exc:
            log.error("Spoken reminder failed: %s", exc)
            handled = (f"I couldn't set that reminder: {exc}", None)
        if handled is not None:
            self._finish_reminder_reply(session_id, text, handled, nova_voice.speakable(handled[0], 300))
            return
        command = nova_voice.command_text(text)                 # without the punctuation whisper adds
        try:
            result = (nova_voice.quick_answer(command)
                      or handle_productivity_command(command, with_db(nova_context.session_project, session_id))
                      or run_local_action(command))
        except Exception as exc:
            result = f"Couldn't complete that: {exc}"
        if result is not None:
            self._online_reply(session_id, text, result, nova_voice.speakable(result, 300))
            return
        self._run_agent(session_id, text, text, True, voice=True)

    def _transcript_confirmed(self, text):
        """Main thread: True to send a transcript — at once if confirming is switched off."""
        if not self.settings.get("voice_confirm_transcript", True):
            return True
        return messagebox.askyesno("Confirm voice input", f"Send this to Nova?\n\n{text}", parent=self.root)

    def _send_transcript(self, text):
        """Main thread: a microphone-button transcript, sent like a typed message."""
        if not self._transcript_confirmed(text):
            self.set_status("Voice input cancelled")
            return
        self.set_status("Transcript ready")
        self.append_message("user", text)
        self.handle_user_message(text, speak=True)

    def _voice_done(self):
        """A request has finished (any thread): the hands-free loop goes back to listening."""
        self.wake_resume_event.set()
        self._agent_voice = False
        loop = self.voice_loop
        if loop is not None:
            loop.finished()
        else:
            VOICE_STATE.to(nova_voice.IDLE, expected=nova_voice.PROCESSING)

    def _voice_cancel(self):
        """Main thread: “cancel” said after the wake phrase."""
        SPEAKER.stop()
        if self.busy and self._agent_cancel is not None and not self._agent_cancel.is_set():
            self._agent_cancel.set()
            self.set_status("Stopping the task after the current step…")
        elif self.busy and self._stop_mode:
            self.stop_reply()
        else:
            self.set_status("Nothing to cancel")

    def stop_speaking(self):
        if SPEAKER.stop():
            self.set_status("Stopped speaking")

    def _on_escape(self, event=None):
        self.stop_speaking()
        return self.stop_reply(event)

    def toggle_voice_pause(self):
        """Pause hands-free listening (the microphone closes) or resume it."""
        if not self.wake_active:
            return
        if self.voice_paused.is_set():
            self.voice_paused.clear()
            self.set_status("Listening resumed")
        else:
            self.voice_paused.set()
            self.set_status("Listening paused • microphone off")
        self.set_controls_enabled(not self.busy)
        self._show_voice_state()
        self._refresh_tray_menu()

    VOICE_STATE_TEXT = {
        nova_voice.IDLE: ("Listening for “{phrase}”", "accent"),
        nova_voice.WAKE_DETECTED: ("Heard you", "warning"),
        nova_voice.LISTENING: ("Listening…", "warning"),
        nova_voice.PROCESSING: ("Working…", "warning"),
        nova_voice.SPEAKING: ("Speaking", "accent"),
        nova_voice.WAITING_FOR_APPROVAL: ("Waiting for your answer", "warning"),
        nova_voice.ERROR: ("Microphone problem", "danger"),
    }

    def _show_voice_state(self, state=None):
        """Main thread: the microphone / listening indicator next to the Hey Nova switch."""
        state = state or VOICE_STATE.state
        button = getattr(self, "voice_state_button", None)
        if button is None:
            return
        colors = {"accent": ACCENT, "warning": WARNING, "danger": DANGER}
        try:
            if self.wake_active:
                if self.voice_paused.is_set():
                    text, color = "\u23f8  Paused • click to resume", MUTED
                else:
                    label, tone = self.VOICE_STATE_TEXT.get(state, ("", "accent"))
                    text, color = "\u25cf  " + label.format(phrase=phrase_label(self.settings)), colors[tone]
                button.configure(text=text, text_color=color)
                if not button.winfo_ismapped():
                    button.pack(side="left", padx=(8, 0))
            elif button.winfo_ismapped():
                button.pack_forget()
            speaking = state == nova_voice.SPEAKING or SPEAKER.speaking
            if speaking and not self.stop_speaking_button.winfo_ismapped():
                self.stop_speaking_button.pack(side="left", padx=(8, 0))
            elif not speaking and self.stop_speaking_button.winfo_ismapped():
                self.stop_speaking_button.pack_forget()
        except tk.TclError:
            pass

    def _refresh_tray_menu(self):
        icon = getattr(self, "tray_icon", None)
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def _speak_quietly(self, text):
        """Speak on a worker thread; a speech failure is logged, never raised."""
        def worker():
            try:
                speak_text(text)
            except Exception as exc:
                log.warning("Speech output failed: %s", exc)
        threading.Thread(target=worker, daemon=True, name="NovaSpeak").start()

    def wake_listener_running(self):
        return self.wake_thread is not None and self.wake_thread.is_alive()

    def start_voice(self):
        if self.busy:
            return
        # The hands-free listener owns the microphone while active: the button asks it to
        # listen now. It can keep the microphone briefly after being switched off.
        loop = self.voice_loop
        if self.wake_active and loop is not None and not self.voice_paused.is_set():
            loop.trigger()
            self.set_status("Listening…")
            return
        if self.wake_active or self.wake_listener_running():
            self.set_status("Microphone in use by hands-free listening • resume it or turn it off")
            return
        self.busy = True
        self.voice_response = True
        self.set_controls_enabled(False)
        self.set_status("Listening…")
        self.voice_stop_event.clear()
        self.voice_thread = threading.Thread(target=self._voice_worker, daemon=True, name="NovaVoiceInput")
        self.voice_thread.start()

    def _voice_worker(self):
        try:
            transcript = record_and_transcribe(lambda s: self.events.put(("status", s)), self.voice_stop_event)
            self.events.put(("transcript_ready", transcript))
        except Exception as exc:
            self.events.put(("voice_error", str(exc)))

    # -------------------- model replies --------------------
    def begin_generation(self, user_text, speak=False):
        if self.busy:
            return
        self._summary_cancel.set()      # a running chat summary yields to the reply
        self.busy = True
        self.voice_response = speak
        self.set_controls_enabled(False)
        self.set_status("Thinking…")
        self.events.put(("assistant_start", None))
        self._start_reply_progress()
        # Read the model once per request so a Settings change mid-reply
        # applies cleanly to the next message.
        model = active_model()
        threading.Thread(
            target=self._generation_worker, args=(self.session_id, user_text, speak, model), daemon=True,
        ).start()

    def _generation_worker(self, session_id, user_text, speak, model):
        started = time.perf_counter()
        cancel = _cancel_event(self)
        try:
            user_message_id = save_message_safe(session_id, "user", user_text)
            # Only what's relevant to this message is gathered (nova_context), within
            # a budget that fits the model's context window.
            budget = context_budget(model, getattr(self, "settings", None))
            ctx = build_context(session_id, user_text, budget)
            if ctx.local_answer:
                # Information only saved data could provide is missing: ask, don't guess.
                self.events.put(("reply_started", None))
                self.events.put(("token", ctx.local_answer))
                message_id = save_message_safe(session_id, "assistant", ctx.local_answer)
                record_context(self, ctx, message_id)
                if speak:
                    try:
                        speak_text(ctx.local_answer)
                    except Exception as exc:
                        log.warning("Speech output failed: %s", exc)
                self.events.put(("generation_done", None))
                return
            messages = ctx.messages()
            nova_perf.record("context: gathering for one reply", (time.perf_counter() - started) * 1000)
            response_parts, first, final, stopped = [], None, None, False
            stream = ollama.chat(
                model=model,
                messages=messages,
                stream=True,
                options={"num_ctx": budget.num_ctx},
            )
            chunks = cancellable_stream(stream, cancel)
            try:
                for chunk in chunks:
                    if cancel.is_set():
                        break
                    piece = _stream_piece(chunk)
                    if piece:
                        if first is None:
                            first = time.perf_counter() - started
                            self.events.put(("reply_started", None))
                        response_parts.append(piece)
                        self.events.put(("token", piece))
                    if _chunk_done(chunk):
                        final = chunk
            finally:
                chunks.close()   # the reader closes the connection, so Ollama stops generating
            stopped = cancel.is_set() and final is None
            record_reply_timings("reply", started, first, final)
            raw_answer = "".join(response_parts)
            answer = raw_answer.strip()
            if stopped:
                self.events.put(("token", "\n\n_(Stopped.)_"))
                answer = (answer + "\n\n" if answer else "") + "_(Stopped.)_"
            elif not answer:
                answer = "I couldn't generate a response. Please try again."
            spoken = answer
            if ctx.documents is not None and not stopped:
                # The source list is built by Nova from what was really sent, not by the model.
                footer = nova_documents.format_sources(ctx.documents, answer)
                self.events.put(("token", "\n\n" + footer))
                answer += "\n\n" + footer
            message_id = save_message_safe(session_id, "assistant", answer)
            record_context(self, ctx, message_id)
            try:   # replayed exactly next time, so Ollama can reuse its cached prompt
                with_db(nova_context.save_sent, {user_message_id: messages[-1]["content"], message_id: raw_answer})
            except Exception as exc:
                log.warning("Couldn't store the sent messages: %s", exc)
            if speak:
                try:
                    speak_text(spoken)
                except Exception as exc:
                    log.warning("Speech output failed: %s", exc)
                    self.events.put(("system", f"Response generated, but speech output failed: {exc}"))
            self.events.put(("generation_done", None))
        except sqlite3.Error as exc:
            log.error("Chat history couldn't be saved: %s", exc)
            self.events.put(("generation_error", f"Nova couldn't save to its database: {exc}\n\n"
                                                 "Settings › Diagnostics shows the database status."))
        except Exception as exc:
            log.error("Generation with model %s failed: %s", model, exc)
            self.events.put(("generation_error", nova_diagnostics.describe_ollama_error(exc, model)))

    def set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.send_button.configure(state=state, fg_color=ACCENT if enabled else SURFACE_2)
        # Voice and Hey Nova share the microphone; never allow both at once.
        self.voice_button.configure(
            state="normal" if enabled and not (self.wake_active and self.voice_paused.is_set()) else "disabled"
        )
        self.input_text.configure(state=state)

    def poll_events(self):
        tokens = []   # consecutive tokens are drawn in one go (one insert, one scroll)
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "token":
                    tokens.append(payload)
                    continue
                if tokens:
                    self.chat.stream("".join(tokens))
                    tokens = []
                if event == "status":
                    self.set_status(payload)
                elif event == "system":
                    self.append_message("system", payload)
                elif event == "assistant_start":
                    self.chat.begin_assistant()
                elif event == "reply_started":
                    self._reply_started = True
                elif event == "doc_reply_started":
                    self._start_reply_progress()
                elif event == "action_done":
                    self.finish_action(*payload)
                elif event == "action_none":
                    # Not a local command: hand it to the model.
                    self.busy = False
                    text, speak = payload
                    self.begin_generation(text, speak=speak)
                elif event == "wake_transcript_ready":
                    # The wake path never sets busy, so it must not clear it:
                    # a typed request may have started while it was recording.
                    self._voice_command(payload)
                elif event == "voice_state":
                    self._show_voice_state(payload)
                elif event == "voice_control":
                    if payload == "cancel":
                        self._voice_cancel()
                elif event == "voice_approval":
                    approval, value = payload
                    decide = self._voice_approvals.pop(approval, None)
                    if decide is not None:
                        decide(value)
                elif event == "wake_setup_error":
                    log.error("Wake-word listener failed: %s", payload)
                    self.wake_active = False
                    self.wake_stop_event.set()
                    VOICE_STATE.reset()
                    self._show_voice_state()
                    self._sync_wake_switch()
                    self.set_controls_enabled(not self.busy)
                    self.set_status("Wake-word setup failed")
                    # The switch now shows "off", so the saved preference follows it.
                    self._persist_setting("wake_word_enabled", False)
                    if self._wake_autostart:
                        self.append_message(
                            "system",
                            f"“Hey Nova” couldn't start, so it has been turned off: {payload}",
                        )
                    else:
                        messagebox.showerror("Hey Nova setup", payload, parent=self.root)
                    self._wake_autostart = False
                elif event == "transcript_ready":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self._send_transcript(payload)
                elif event == "voice_error":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Voice input failed")
                    messagebox.showerror("Voice input", payload, parent=self.root)
                elif event == "reminders_changed":
                    self._refresh_reminder_views()
                elif event == "task_plan":
                    self._review_task(*payload)
                elif event in ("task_update", "task_confirm", "task_done"):
                    self._handle_task_event(event, payload)
                elif event == "memory_remember":
                    self._confirm_remember(*payload)
                elif event == "memory_forget":
                    self._confirm_forget(*payload)
                elif event == "memory_suggested":
                    self.append_message(
                        "system",
                        f"Nova noticed {payload} thing{'s' if payload != 1 else ''} you might want it to "
                        "remember. Nothing was saved: review it in Memories › Review.",
                    )
                elif event == "doc_progress":
                    self.doc_status = payload
                    if self._documents_ui:
                        self._documents_ui["status"](payload)
                elif event == "doc_changed":
                    if self._documents_ui:
                        self._documents_ui["refresh"]()
                elif event == "context_link":
                    self._pending_link = payload
                elif event == "agent_event":
                    self._agent_event(*payload)
                elif event == "agent_ask":
                    self._agent_answer(*payload)
                elif event == "online_consent":
                    self._confirm_online(*payload)
                elif event == "context_inspect":
                    self.open_context_inspector(*payload)
                elif event == "project_command":
                    self._project_command(*payload)
                elif event == "generation_done":
                    self._finish_reply_progress()
                    self.chat.end_assistant()
                    self._show_pending_link()
                    self._schedule("summarize", 1500, self._maybe_summarize)
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Ready")
                    self.refresh_sessions(select_current=True)
                    self._voice_done()
                elif event == "generation_error":
                    self._finish_reply_progress()
                    self.chat.end_assistant()
                    self._pending_link = None
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Error • see the message")
                    self.chat.add_info(payload.split("\n")[0], error=True)
                    messagebox.showerror("Nova", payload, parent=self.root)
                    self._voice_done()
        except queue.Empty:
            pass
        if tokens:
            self.chat.stream("".join(tokens))
        # Every request ends with Nova no longer busy, whichever way it went (dialogs
        # included), so a hands-free request still “working” then is finished.
        if (not self.busy and not self._voice_handoff and VOICE_STATE.state == nova_voice.PROCESSING
                and not SPEAKER.speaking):
            self._voice_done()
        # Every 100 ms while Nova is working, 250 ms when idle.
        self._schedule("events", 100 if self.busy else 250, self.poll_events)

    # -------------------- dialogs: shared pieces --------------------
    def _dialog(self, title, geometry, minsize=None, resizable=True):
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title(f"{title} — Nova")
        win.geometry(geometry)
        if minsize:
            win.minsize(*minsize)
        if not resizable:
            win.resizable(False, False)
        win.transient(self.root)
        self._apply_icon(win)
        # CTkToplevel can open behind the main window on Windows.
        win.after(80, lambda: (win.lift(), win.focus_force()))
        return win

    def _dialog_header(self, parent, title, subtitle):
        ctk.CTkLabel(parent, text=title, font=self.fonts.heading(24), text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(
            parent, text=subtitle, font=self.fonts.ui(13), text_color=MUTED,
            anchor="w", justify="left", wraplength=640,
        ).pack(fill="x", pady=(3, 16))

    def _entry(self, master, placeholder, width=200, font_size=14, **kwargs):
        options = dict(
            placeholder_text=placeholder, width=width, height=40, corner_radius=10,
            fg_color=SURFACE_2, border_color=BORDER, border_width=1, text_color=TEXT,
            placeholder_text_color=SUBTLE, font=self.fonts.ui(font_size),
        )
        options.update(kwargs)
        return ctk.CTkEntry(master, **options)

    def _button(self, master, text, command, kind="primary", width=110):
        fg, hover, text_color = {
            "primary": (ACCENT, ACCENT_HOVER, "#ffffff"),
            "secondary": (SURFACE_2, BORDER, TEXT),
            "danger": (DANGER_SOFT, "#4a272c", DANGER),
        }[kind]
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=40, corner_radius=10,
            fg_color=fg, hover_color=hover, text_color=text_color,
            font=self.fonts.ui(14, semibold=True),
        )

    def _chip(self, master, text, fg, text_color):
        return ctk.CTkLabel(
            master, text=f"  {text}  ", font=self.fonts.ui(12, semibold=True),
            fg_color=fg, text_color=text_color, corner_radius=8, height=22,
        )

    def _scroll_list(self, master):
        frame = ctk.CTkScrollableFrame(
            master, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=SURFACE_2, scrollbar_button_hover_color=BORDER,
        )
        autohide_scrollbar(frame)
        return frame

    def _empty_state(self, master, glyph, title, subtitle):
        box = ctk.CTkFrame(master, fg_color="transparent")
        box.pack(fill="x", pady=40)
        ctk.CTkLabel(box, text=glyph, font=self.fonts.icon(28), text_color=SUBTLE).pack()
        ctk.CTkLabel(box, text=title, font=self.fonts.ui(15, semibold=True), text_color=TEXT).pack(pady=(10, 2))
        ctk.CTkLabel(box, text=subtitle, font=self.fonts.ui(13), text_color=MUTED, wraplength=360).pack()

    # -------------------- Memories --------------------
    def _memory_save_checks(self, parent, content, exclude_id=None):
        """Warn about near-duplicates and instruction-like text; True to go ahead."""
        similar = [(m, score) for m, score in with_db(nova_memories.find_similar, content, exclude_id=exclude_id)
                   if nova_memories.dedupe_key(m.content) != nova_memories.dedupe_key(content)]
        if similar:
            listing = "\n".join(f"• [{m.category}] {m.content[:140]}" for m, _score in similar[:3])
            if not messagebox.askyesno(
                "Similar memory",
                f"You already have similar memories:\n\n{listing}\n\nSave this as a separate memory anyway? "
                "(Nothing is merged or deleted automatically.)",
                parent=parent,
            ):
                return False
        if nova_memories.looks_like_instruction(content):
            if not messagebox.askyesno(
                "Instruction-like memory",
                "This reads like an instruction to Nova rather than a fact about you.\n\n"
                "Memories are only ever used as information about you: they can't change Nova's rules, "
                "permissions or safety behavior, and Nova is told to ignore instructions inside them.\n\n"
                "Save it anyway?",
                icon="warning", parent=parent,
            ):
                return False
        return True

    def _confirm_remember(self, session_id, text, content, speak):
        """'remember that …' in chat: ask before saving anything."""
        try:
            identical = with_db(nova_memories.find_identical, content)
            if identical:
                reply = f"That's already in my long-term memory: “{identical.content}”."
            else:
                self.set_status("Confirm memory")
                question = (f"Save this to Nova's long-term memory?\n\n“{content}”\n\n"
                            "You can review, pin, edit or delete it any time in Memories.")
                if not messagebox.askyesno("Remember", question, parent=self.root):
                    reply = "Okay, I didn't save that."
                elif not self._memory_save_checks(self.root, content):
                    reply = "Okay, I didn't save that."
                else:
                    with_db(nova_memories.add_memory, content, source="chat")
                    reply = f"Saved to long-term memory: “{content}”. You can review or edit it in Memories."
                    log.info("Memory saved from chat")
        except ValueError as exc:
            reply = f"I couldn't save that: {exc}"
        except Exception as exc:
            log.error("Saving memory from chat failed: %s", exc)
            reply = f"I couldn't save that memory: {exc}"
        self._finish_memory_command(session_id, text, reply, speak)

    def _confirm_forget(self, session_id, text, query, speak):
        """'forget …' in chat: show the matching memories and ask before deleting."""
        try:
            matches = with_db(nova_memories.find_for_forget, query)
            if not matches:
                reply = (f"I couldn't find a saved memory matching “{query}”. "
                         "Open Memories to see everything I remember.")
            else:
                listing = "\n".join(f"• [{m.category}] {m.content[:160]}" for m in matches)
                count = len(matches)
                self.set_status("Confirm deletion")
                if messagebox.askyesno(
                    "Forget",
                    f"Delete {'this memory' if count == 1 else f'these {count} memories'} from long-term "
                    f"memory?\n\n{listing}\n\nYour conversations aren't affected.",
                    icon="warning", parent=self.root,
                ):
                    for memory in matches:
                        with_db(nova_memories.delete_memory, memory.id)
                    reply = f"Deleted {count} memor{'y' if count == 1 else 'ies'} from long-term memory."
                    log.info("Deleted %d memories from chat", count)
                else:
                    reply = "Okay, nothing was deleted."
        except Exception as exc:
            log.error("Forget command failed: %s", exc)
            reply = f"I couldn't change my memories: {exc}"
        self._finish_memory_command(session_id, text, reply, speak)

    def _finish_memory_command(self, session_id, text, reply, speak):
        try:
            save_message_safe(session_id, "user", text)
            save_message_safe(session_id, "assistant", reply)
        except Exception as exc:
            reply += f"\n\n(Note: couldn't save this exchange to chat history: {exc})"
        self.finish_action(reply, speak)
        self._refresh_memory_window()

    def _refresh_memory_window(self):
        ui = getattr(self, "_memory_ui", None)
        if ui:
            ui()

    def _memory_editor(self, parent, memory=None, initial=None, suggestion_id=None, on_saved=None):
        """Dialog to create or edit a memory (or edit a suggestion before saving it)."""
        f = self.fonts
        editing = memory is not None
        title = "Edit memory" if editing else ("Save suggestion" if suggestion_id else "New memory")
        win = ctk.CTkToplevel(parent, fg_color=BG)
        win.title(f"{title} — Nova")
        win.geometry("600x500")
        win.minsize(500, 440)
        win.transient(parent)
        self._apply_icon(win)
        win.after(80, lambda: (win.lift(), win.focus_force()))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=24, pady=20)
        ctk.CTkLabel(body, text=title, font=f.heading(20), text_color=TEXT, anchor="w").pack(fill="x")

        content0, category0 = initial or ((memory.content, memory.category) if editing else ("", "general"))
        ctk.CTkLabel(body, text="Category", font=f.ui(13, semibold=True), text_color=MUTED,
                     anchor="w").pack(fill="x", pady=(14, 4))
        category = self._entry(body, "e.g. profile, preference, work", width=260)
        category.insert(0, category0)
        category.pack(anchor="w")
        ctk.CTkLabel(body, text="What should Nova remember?", font=f.ui(13, semibold=True), text_color=MUTED,
                     anchor="w").pack(fill="x", pady=(12, 4))
        text_box = ctk.CTkTextbox(body, height=110, fg_color=SURFACE_2, text_color=TEXT, font=f.ui(14),
                                  wrap="word", border_width=1, border_color=BORDER, corner_radius=10)
        text_box.insert("1.0", content0)
        text_box.pack(fill="x")
        flags = ctk.CTkFrame(body, fg_color="transparent")
        flags.pack(fill="x", pady=(12, 0))
        pin_var = tk.BooleanVar(value=bool(memory and memory.pinned))
        include_var = tk.BooleanVar(value=True if memory is None else memory.include)
        for label, variable in (("Pin (always give this to Nova)", pin_var),
                                ("Use in Nova's replies", include_var)):
            ctk.CTkCheckBox(flags, text=label, variable=variable, font=f.ui(13), text_color=TEXT,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER,
                            checkbox_width=18, checkbox_height=18).pack(side="left", padx=(0, 18))
        hint = ctk.CTkLabel(body, text="", font=f.ui(12), text_color=WARNING, anchor="w", justify="left",
                            wraplength=540)
        hint.pack(fill="x", pady=(10, 0))
        counter = ctk.CTkLabel(body, text="", font=f.ui(11), text_color=SUBTLE, anchor="e")
        counter.pack(fill="x")
        pending = {"job": None}

        def check():
            pending["job"] = None
            content = text_box.get("1.0", "end").strip()
            counter.configure(text=f"{len(content)} / {nova_memories.MAX_MEMORY_CHARS:,}")
            notes = []
            try:
                similar = with_db(nova_memories.find_similar, content, exclude_id=memory.id if editing else None)
            except Exception:
                similar = []
            for other, score in similar[:2]:
                same = nova_memories.dedupe_key(other.content) == nova_memories.dedupe_key(content)
                notes.append(f"{'Already saved' if same else f'Similar ({score:.0%})'}: “{other.content[:120]}”")
            if nova_memories.looks_like_instruction(content):
                notes.append("Reads like an instruction. Memories are used only as facts about you and can't "
                             "change Nova's rules or permissions.")
            hint.configure(text="\n".join(notes))

        def schedule(_event=None):
            if pending["job"]:
                win.after_cancel(pending["job"])
            pending["job"] = win.after(300, check)

        text_box.bind("<KeyRelease>", schedule)

        def save():
            content = text_box.get("1.0", "end").strip()
            changed = not editing or nova_memories.dedupe_key(content) != nova_memories.dedupe_key(memory.content)
            try:
                if changed and not self._memory_save_checks(win, content, exclude_id=memory.id if editing else None):
                    return
                if editing:
                    with_db(nova_memories.update_memory, memory.id, content, category.get(),
                            pinned=pin_var.get(), include=include_var.get())
                    status = "Memory updated"
                else:
                    if suggestion_id is not None:
                        result = with_db(nova_memories.accept_suggestion, suggestion_id, content, category.get())
                    else:
                        result = with_db(nova_memories.add_memory, content, category.get(),
                                         pinned=pin_var.get(), include=include_var.get())
                    if result.created:
                        with_db(nova_memories.update_memory, result.memory.id,
                                pinned=pin_var.get(), include=include_var.get())
                        status = "Memory saved"
                    else:
                        status = "Already saved • moved to top"
                        messagebox.showinfo("Memories", "An identical memory is already saved; it was moved to "
                                                        "the top instead of being duplicated.", parent=win)
            except ValueError as exc:
                messagebox.showwarning("Memories", str(exc), parent=win)
                return
            except sqlite3.Error as exc:
                log.error("Saving memory failed: %s", exc)
                messagebox.showerror("Memories", f"Couldn't save the memory: {exc}", parent=win)
                return
            self.set_status(status)
            win.destroy()
            if on_saved:
                on_saved()

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        self._button(buttons, "Save", save, width=100).pack(side="right")
        self._button(buttons, "Cancel", win.destroy, kind="secondary", width=100).pack(side="right", padx=8)
        check()
        win.after(150, lambda: text_box.focus_set())
        return win

    def open_memories(self):
        """Memory manager: browse/search/edit, review suggestions and duplicates, preview prompts."""
        existing = getattr(self, "_memory_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

        f = self.fonts
        win = self._dialog("Memories", "960x760", (820, 600))
        self._memory_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(
            body, "Long-term memory",
            "Facts you choose to keep about yourself. Nova adds the most relevant ones to each reply. This is "
            "separate from conversation history: deleting a memory never deletes a chat, and deleting a chat "
            "never deletes a memory. Nothing is saved here without your approval.",
        )
        tabs = ctk.CTkTabview(
            body, fg_color=BG, corner_radius=12, anchor="w",
            segmented_button_fg_color=SURFACE, segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=SURFACE, segmented_button_unselected_hover_color=SURFACE_2,
            text_color=TEXT,
        )
        tabs.pack(fill="both", expand=True)
        list_tab, review_tab, preview_tab = tabs.add("Memories"), tabs.add("Review"), tabs.add("What Nova sees")
        try:
            tabs._segmented_button.configure(font=f.ui(13, semibold=True), height=34)
        except Exception:
            pass

        def guarded(action, *args, success=None):
            try:
                result = with_db(action, *args)
            except ValueError as exc:
                messagebox.showwarning("Memories", str(exc), parent=win)
                return None
            except sqlite3.Error as exc:
                log.error("Memory action failed: %s", exc)
                messagebox.showerror("Memories", f"The memory database couldn't be updated: {exc}", parent=win)
                return None
            if success:
                self.set_status(success)
            refresh()
            return result

        def meta_text(memory):
            parts = [f"Created {format_timestamp(memory.created_at)}"]
            if memory.updated_at and memory.updated_at != memory.created_at:
                parts.append(f"updated {format_timestamp(memory.updated_at)}")
            if memory.use_count:
                parts.append(f"used in {memory.use_count} repl{'y' if memory.use_count == 1 else 'ies'}, last "
                             f"{format_timestamp(memory.last_used_at)}")
            else:
                parts.append("not used in a reply yet")
            if memory.source not in ("manual", "cli"):
                parts.append({"suggested": "from a suggestion", "chat": "saved from chat"}.get(memory.source,
                                                                                         memory.source))
            return " · ".join(parts)

        def delete(memory):
            if messagebox.askyesno(
                "Delete memory",
                f"Delete this memory permanently?\n\n“{memory.content[:300]}”\n\nYour conversations aren't affected.",
                icon="warning", parent=win,
            ):
                guarded(nova_memories.delete_memory, memory.id, success="Memory deleted")

        def memory_card(parent, memory, actions=True, extra=None):
            card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
            card.pack(fill="x", pady=4, padx=(0, 8))
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(10, 0))
            self._chip(top, memory.category, ACCENT_SOFT, ACCENT).pack(side="left")
            if memory.pinned:
                self._chip(top, "Pinned", "#1d3326", SUCCESS).pack(side="left", padx=(6, 0))
            if not memory.include:
                self._chip(top, "Not used in replies", "#3a3220", WARNING).pack(side="left", padx=(6, 0))
            elif nova_memories.looks_like_instruction(memory.content):
                blocked = self._chip(top, "Not sent: reads like an instruction", DANGER_SOFT, DANGER)
                blocked.pack(side="left", padx=(6, 0))
                Tooltip(blocked, "Rephrase it as a fact about you (e.g. “I prefer …”) to use it", f)
            if actions:
                for glyph, tip, command, hover in (
                    (Icon.DELETE, "Delete", lambda: delete(memory), DANGER_SOFT),
                    (Icon.EDIT, "Edit", lambda: self._memory_editor(win, memory, on_saved=refresh), SURFACE_2),
                    (Icon.VIEW if memory.include else Icon.HIDE,
                     "Exclude from Nova's replies" if memory.include else "Use in Nova's replies again",
                     lambda: guarded(nova_memories.update_memory, memory.id, None, None, None, not memory.include,
                                     success="Memory excluded from replies" if memory.include
                                     else "Memory used in replies again"), SURFACE_2),
                    (Icon.UNPIN if memory.pinned else Icon.PIN, "Unpin" if memory.pinned else
                     "Pin: always give this to Nova",
                     lambda: guarded(nova_memories.update_memory, memory.id, None, None, not memory.pinned,
                                     success="Memory unpinned" if memory.pinned else "Memory pinned"), SURFACE_2),
                ):
                    button = icon_button(top, f, glyph, command, size=28, icon_size=12, hover=hover,
                                         text_color=SUBTLE)
                    button.pack(side="right", padx=1)
                    Tooltip(button, tip, f)
            for child in extra or ():
                child(top)
            ctk.CTkLabel(card, text=memory.content, font=f.ui(14), text_color=TEXT if memory.include else MUTED,
                         anchor="w", justify="left", wraplength=780).pack(fill="x", padx=14, pady=(6, 0))
            ctk.CTkLabel(card, text=meta_text(memory), font=f.ui(11), text_color=SUBTLE,
                         anchor="w").pack(fill="x", padx=14, pady=(2, 10))
            return card

        # ==================== Memories tab ====================
        toolbar = ctk.CTkFrame(list_tab, fg_color="transparent")
        toolbar.pack(fill="x", pady=(6, 4))
        search = self._entry(toolbar, "Search memories…", width=320)
        search.pack(side="left")
        views = {"All": "all", "Pinned": "pinned", "Excluded": "excluded", "Never used": "unused"}
        view_var = tk.StringVar(value="All")
        ctk.CTkSegmentedButton(
            toolbar, values=list(views), variable=view_var, command=lambda _v: refresh(), font=f.ui(12),
            fg_color=SURFACE, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=SURFACE, unselected_hover_color=SURFACE_2, text_color=TEXT, height=34,
        ).pack(side="left", padx=10)
        self._button(toolbar, "+  New memory", lambda: self._memory_editor(win, on_saved=refresh),
                     width=140).pack(side="right")
        summary = ctk.CTkLabel(list_tab, text="", font=f.ui(12), text_color=MUTED, anchor="w")
        summary.pack(fill="x", padx=2, pady=(2, 4))
        memory_list = self._scroll_list(list_tab)
        memory_list.pack(fill="both", expand=True)

        def render_list():
            for child in memory_list.winfo_children():
                child.destroy()
            query = search.get().strip()
            try:
                memories = with_db(nova_memories.list_memories, query, views[view_var.get()])
                totals = with_db(nova_memories.counts)
            except sqlite3.Error as exc:
                self._empty_state(memory_list, Icon.WARNING, "Couldn't load memories", str(exc))
                return
            summary.configure(text=(
                f"{totals['all']} memor{'y' if totals['all'] == 1 else 'ies'} · {totals['pinned']} pinned · "
                f"{totals['excluded']} excluded from replies"
                + (f" · {len(memories)} shown" if query or view_var.get() != "All" else "")))
            if not memories:
                if query or view_var.get() != "All":
                    self._empty_state(memory_list, Icon.SEARCH, "No matching memories", "Try another word or view.")
                else:
                    self._empty_state(memory_list, Icon.MEMORY, "No memories yet",
                                      "Add facts about yourself, like your name or how you like answers. "
                                      "You can also say “remember that …” in a chat.")
                return
            for memory in memories[:150]:
                memory_card(memory_list, memory)
            if len(memories) > 150:
                ctk.CTkLabel(memory_list, text=f"Showing 150 of {len(memories)}. Search to narrow the list.",
                             font=f.ui(12), text_color=SUBTLE).pack(pady=8)

        pending_search = {"job": None}

        def on_search(_event=None):
            if pending_search["job"]:
                win.after_cancel(pending_search["job"])
            pending_search["job"] = win.after(200, render_list)

        search.bind("<KeyRelease>", on_search)

        # ==================== Review tab ====================
        review_list = self._scroll_list(review_tab)
        review_list.pack(fill="both", expand=True, pady=(6, 0))
        suggestions_var = tk.BooleanVar(value=bool(self.settings.get("memory_suggestions_enabled")))

        def toggle_suggestions():
            self._persist_setting("memory_suggestions_enabled", bool(suggestions_var.get()))
            self.set_status("Memory suggestions on" if suggestions_var.get() else "Memory suggestions off")

        def section(title, subtitle=None):
            ctk.CTkLabel(review_list, text=title, font=f.ui(15, semibold=True), text_color=TEXT,
                         anchor="w").pack(fill="x", pady=(14, 0))
            if subtitle:
                ctk.CTkLabel(review_list, text=subtitle, font=f.ui(12), text_color=MUTED, anchor="w",
                             justify="left", wraplength=820).pack(fill="x", pady=(2, 4))

        def small_button(parent, text, command, kind="secondary", width=90):
            button = ctk.CTkButton(
                parent, text=text, command=command, width=width, height=30, corner_radius=8,
                font=f.ui(12, semibold=True),
                fg_color={"primary": ACCENT, "secondary": SURFACE_2, "danger": DANGER_SOFT}[kind],
                hover_color={"primary": ACCENT_HOVER, "secondary": BORDER, "danger": "#4a272c"}[kind],
                text_color={"primary": "#ffffff", "secondary": TEXT, "danger": DANGER}[kind],
            )
            button.pack(side="right", padx=(6, 0))
            return button

        def render_review():
            for child in review_list.winfo_children():
                child.destroy()
            try:
                suggestions = with_db(nova_memories.pending_suggestions)
                pairs = with_db(nova_memories.possible_duplicates)
                stale = with_db(nova_memories.stale_memories)
            except sqlite3.Error as exc:
                self._empty_state(review_list, Icon.WARNING, "Couldn't load the review", str(exc))
                return

            section("Suggestions from your messages",
                    "Off unless you turn it on. Nova looks for statements like “My name is…” or “I prefer…” in "
                    "your own chat messages (never in documents or Nova's replies) and lists them here. "
                    "Nothing is saved until you choose Save.")
            toggle_row = ctk.CTkFrame(review_list, fg_color=SURFACE, corner_radius=12)
            toggle_row.pack(fill="x", pady=4, padx=(0, 8))
            ctk.CTkSwitch(
                toggle_row, text="Suggest memories from what I tell Nova", variable=suggestions_var,
                onvalue=True, offvalue=False, command=toggle_suggestions, font=f.ui(13), text_color=TEXT,
                progress_color=ACCENT, fg_color=SURFACE_2, button_color=TEXT, button_hover_color="#ffffff",
                switch_width=40, switch_height=22,
            ).pack(anchor="w", padx=14, pady=12)
            for suggestion in suggestions:
                card = ctk.CTkFrame(review_list, fg_color=SURFACE, corner_radius=12)
                card.pack(fill="x", pady=4, padx=(0, 8))
                top = ctk.CTkFrame(card, fg_color="transparent")
                top.pack(fill="x", padx=14, pady=(10, 0))
                self._chip(top, suggestion.category, ACCENT_SOFT, ACCENT).pack(side="left")
                ctk.CTkLabel(top, text=f"Suggested {format_timestamp(suggestion.created_at)}", font=f.ui(11),
                             text_color=SUBTLE).pack(side="left", padx=8)
                small_button(top, "Dismiss",
                             lambda s=suggestion: guarded(nova_memories.dismiss_suggestion, s.id,
                                                          success="Suggestion dismissed"))
                small_button(top, "Edit…", lambda s=suggestion: self._memory_editor(
                    win, initial=(s.content, s.category), suggestion_id=s.id, on_saved=refresh))
                small_button(top, "Save", lambda s=suggestion: guarded(
                    nova_memories.accept_suggestion, s.id, success="Memory saved"), kind="primary")
                ctk.CTkLabel(card, text=suggestion.content, font=f.ui(14), text_color=TEXT, anchor="w",
                             justify="left", wraplength=780).pack(fill="x", padx=14, pady=(6, 0))
                ctk.CTkLabel(card, text=f"From your message: “{suggestion.evidence[:200]}”", font=f.ui(11),
                             text_color=SUBTLE, anchor="w", justify="left", wraplength=780).pack(
                    fill="x", padx=14, pady=(2, 10))
            if not suggestions:
                ctk.CTkLabel(review_list, text="No suggestions waiting.", font=f.ui(12), text_color=SUBTLE,
                             anchor="w").pack(fill="x", padx=4)

            section("Possible duplicates",
                    "Memories with very similar wording. Nova never merges or deletes them on its own: keep "
                    "both, or delete the one you don't need.")
            for first, second, score in pairs:
                holder = ctk.CTkFrame(review_list, fg_color="transparent")
                holder.pack(fill="x")
                bar = ctk.CTkFrame(holder, fg_color="transparent")
                bar.pack(fill="x", padx=(0, 8))
                ctk.CTkLabel(bar, text=f"{score:.0%} similar", font=f.ui(12, semibold=True), text_color=WARNING,
                             anchor="w").pack(side="left")
                small_button(bar, "Keep both", lambda a=first, b=second: guarded(
                    nova_memories.keep_both, a.id, b.id, success="Kept both memories"))
                memory_card(holder, first)
                memory_card(holder, second)
            if not pairs:
                ctk.CTkLabel(review_list, text="No possible duplicates.", font=f.ui(12), text_color=SUBTLE,
                             anchor="w").pack(fill="x", padx=4)

            section(f"Not reviewed for {nova_memories.STALE_DAYS // 30} months",
                    "Still true? Confirm, edit or delete older memories so Nova doesn't rely on outdated facts.")
            for memory in stale[:30]:
                memory_card(review_list, memory, extra=[lambda top, m=memory: small_button(
                    top, "Still true", lambda: guarded(nova_memories.mark_reviewed, m.id,
                                                       success="Marked as reviewed"))])
            if not stale:
                ctk.CTkLabel(review_list, text="Everything has been reviewed recently.", font=f.ui(12),
                             text_color=SUBTLE, anchor="w").pack(fill="x", padx=4)

        # ==================== What Nova sees tab ====================
        ctk.CTkLabel(
            preview_tab,
            text=(f"Type a message to see which memories Nova would add to its prompt, and why. Per message: "
                  f"pinned memories first (up to {nova_memories.PROMPT_MAX_PINNED}), then those sharing words "
                  f"with your message, then a few general ones; at most "
                  f"{getattr(backend, 'MAX_MEMORIES_IN_CONTEXT', nova_memories.PROMPT_MAX_MEMORIES)} memories "
                  f"and {nova_memories.PROMPT_MAX_CHARS:,} characters. Excluded memories are never sent."),
            font=f.ui(12), text_color=MUTED, anchor="w", justify="left", wraplength=860,
        ).pack(fill="x", pady=(6, 6))
        probe = self._entry(preview_tab, "e.g. Can you suggest a dinner recipe?", width=520)
        probe.pack(anchor="w")
        preview = tk.Text(
            preview_tab, wrap="word", bg=BG, fg=TEXT, relief="flat", borderwidth=0, highlightthickness=0,
            font=f.tk(13), padx=4, pady=8, cursor="arrow", insertwidth=0,
            selectbackground=SELECTION, inactiveselectbackground=SELECTION,
        )
        preview.pack(fill="both", expand=True, pady=(8, 0))
        preview.tag_configure("head", font=f.tk(13, True), foreground=TEXT, spacing1=10, spacing3=4)
        preview.tag_configure("yes", foreground=SUCCESS)
        preview.tag_configure("no", foreground=SUBTLE)
        preview.tag_configure("why", foreground=MUTED, font=f.tk(12), lmargin1=26, lmargin2=26)
        preview.tag_configure("block", font=f.tk_mono(12), foreground="#d7dae0", background=CODE_BG,
                              lmargin1=10, lmargin2=10, rmargin=10, spacing1=1, spacing3=1)

        def render_preview(_event=None):
            message = probe.get().strip()
            try:
                ranked = with_db(lambda con: nova_memories.select(
                    nova_memories.all_memories(con), message,
                    max_items=getattr(backend, "MAX_MEMORIES_IN_CONTEXT", nova_memories.PROMPT_MAX_MEMORIES)))
                excluded = with_db(nova_memories.counts)["excluded"]
            except sqlite3.Error as exc:
                ranked, excluded = [], 0
                log.error("Memory preview failed: %s", exc)
            included = [r for r in ranked if r.included]
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            used = sum(len(nova_memories.prompt_line(r.memory)) + 1 for r in included)
            preview.insert("end", f"Included: {len(included)} memories, {used:,} of "
                                  f"{nova_memories.PROMPT_MAX_CHARS:,} characters\n", "head")
            for item in included:
                preview.insert("end", "✓ ", "yes")
                preview.insert("end", f"{item.memory.content[:160]}\n")
                preview.insert("end", " · ".join(item.reasons) + f" · score {item.score:.2f}\n", "why")
            left_out = [r for r in ranked if not r.included and (r.skipped or r.relevance > 0)]
            if left_out:
                preview.insert("end", "Relevant but left out\n", "head")
                for item in left_out[:10]:
                    preview.insert("end", "✗ ", "no")
                    preview.insert("end", f"{item.memory.content[:160]}\n", "no")
                    preview.insert("end", (item.skipped or "lower score") + "\n", "why")
            if excluded:
                preview.insert("end", f"\n{excluded} excluded memor{'y is' if excluded == 1 else 'ies are'} "
                                      "never sent.\n", "why")
            block = nova_memories.build_block(included)
            preview.insert("end", "Exact text added after Nova's rules\n", "head")
            preview.insert("end", (block or "(nothing — no memories would be sent)") + "\n", "block")
            preview.configure(state="disabled")

        probe.bind("<KeyRelease>", lambda _e: win.after_idle(render_preview))

        def refresh():
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            render_list()
            render_review()
            render_preview()

        def on_destroy(event):
            if event.widget is win:
                self._memory_ui = None

        win.bind("<Destroy>", on_destroy, add="+")
        self._memory_ui = refresh
        refresh()
        try:
            if with_db(nova_memories.counts)["suggestions"]:
                tabs.set("Review")
        except sqlite3.Error:
            pass
        win.after(150, search.focus_set)

    # -------------------- Search --------------------
    def open_search_result(self, session_id, message_id=None, terms=()):
        """Open an existing conversation and jump to a message.

        Never creates a conversation. Returns None on success, or a
        user-facing reason it couldn't be opened.
        """
        if session_id != self.session_id:
            if self.busy:
                return "Wait for Nova to finish the current reply before opening another conversation."
            try:
                exists = conversation_exists(session_id)
            except Exception as exc:
                log.error("Checking conversation %s failed: %s", session_id, exc)
                return f"Couldn't open the conversation: {exc}"
            if not exists:
                return "That conversation has been deleted. Search again to refresh the results."
            self._follow_chat_project(session_id)
            self.select_session(session_id)
            if self.session_id != session_id:
                return "Couldn't open that conversation."
        self.show_window()
        if message_id is not None:
            key = f"msg_{message_id}"
            found = self.chat.reveal_message(key, terms)
            if not found and not self.busy:
                # Older than the messages on screen, or added since: draw the part around it.
                self.load_chat(anchor_id=message_id)
                found = self.chat.reveal_message(key, terms)
            if not found:
                self.set_status("Opened conversation • that message is no longer in it")
                return None
        self.set_status("Opened search result")
        self.root.after(50, self.input_text.focus_set)
        return None

    def open_search(self):
        """Search window: titles and messages across all conversations."""
        existing = getattr(self, "_search_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                existing.after(50, lambda: (self._search_entry.focus_set(), self._search_entry.select_range(0, "end")))
                return
        except tk.TclError:
            pass

        f = self.fonts
        win = self._dialog("Search", "780x680", (600, 480))
        self._search_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(
            body, "Search conversations",
            "Finds words in your chat titles and messages, on this PC only. Every word must match; "
            "use “quotes” for an exact phrase. Use ↑ ↓ and Enter to open a result.",
        )

        bar = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text=Icon.SEARCH, font=f.icon(15), text_color=MUTED, width=20).pack(side="left", padx=(16, 2))
        entry = self._entry(bar, "Search chats…", font_size=15, fg_color=SURFACE, border_width=0)
        entry.pack(side="left", fill="x", expand=True, padx=(4, 4), pady=6)
        self._search_entry = entry
        clear_button = icon_button(bar, f, Icon.CLOSE, lambda: clear_query(), size=30, icon_size=11, text_color=SUBTLE)
        clear_button.pack(side="right", padx=(0, 10))
        Tooltip(clear_button, "Clear (Esc)", f)
        status =ctk.CTkLabel(body, text="", font=f.ui(12), text_color=MUTED, anchor="w")
        status.pack(fill="x", padx=4, pady=(8, 2))

        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        more_button = self._button(footer, "Load more", lambda: run_search(append=True), kind="secondary", width=120)

        area = ctk.CTkFrame(body, fg_color="transparent")
        area.pack(fill="both", expand=True)
        text = tk.Text(
            area, wrap="word", bg=BG, fg=TEXT, relief="flat", borderwidth=0, highlightthickness=0,
            padx=0, pady=4, cursor="arrow", font=f.tk(14), insertwidth=0, takefocus=0,
            selectbackground=SELECTION, selectforeground=TEXT, inactiveselectbackground=SELECTION,
        )
        scrollbar = ctk.CTkScrollbar(area, command=text.yview, fg_color=BG, button_color=SURFACE_2,
                                     button_hover_color=BORDER, width=12)

        def on_scroll(first, last):
            scrollbar.set(first, last)
            if float(first) <= 0.0 and float(last) >= 1.0:
                scrollbar.pack_forget()
            elif not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y", before=text)

        text.configure(yscrollcommand=on_scroll, state="disabled")
        text.pack(side="left", fill="both", expand=True)
        text.bind("<MouseWheel>", lambda e: (text.yview_scroll(int(-e.delta / 120), "units"), "break")[1])

        block = dict(lmargin1=16, lmargin2=16, rmargin=16)
        text.tag_configure("section", font=f.tk(12, True), foreground=SUBTLE, spacing1=14, spacing3=6, lmargin1=4)
        text.tag_configure("title", font=f.tk(14, True), foreground=TEXT, spacing1=10, **block)
        text.tag_configure("meta", font=f.tk(12), foreground=SUBTLE)
        text.tag_configure("nova", foreground=ACCENT)
        text.tag_configure("excerpt", font=f.tk(13), foreground=MUTED, spacing1=3, spacing3=10, **block)
        text.tag_configure("empty", font=f.tk(14), foreground=MUTED, justify="center", spacing1=60, spacing3=6)
        text.tag_configure("empty_sub", font=f.tk(13), foreground=SUBTLE, justify="center")
        text.tag_configure("hover", background=SURFACE, lmargincolor=SURFACE, rmargincolor=SURFACE)
        text.tag_configure("active", background=SURFACE_2, lmargincolor=SURFACE_2, rmargincolor=SURFACE_2)
        text.tag_configure("hit", background=FOUND_TERM_BG, foreground="#ffffff")
        text.tag_raise("hit")

        # Worker threads hand results back to Tk's thread through this queue.
        ui_queue = queue.Queue()
        state = {"generation": 0, "query": None, "terms": [], "results": [], "next_before": None,
                 "messages": 0, "sessions": set(), "titles": 0, "active": None, "pending": None}

        def pump():
            try:
                while True:
                    ui_queue.get_nowait()()
            except queue.Empty:
                pass
            except tk.TclError:
                return
            except Exception:
                log.exception("Search window update failed")
            try:
                if win.winfo_exists():
                    win.after(60, pump)
            except tk.TclError:
                pass

        def write(callback):
            text.configure(state="normal")
            try:
                callback()
            finally:
                text.configure(state="disabled")

        def insert_marked(value, spans, tags):
            position = 0
            for start, end in spans:
                if start > position:
                    text.insert("end", value[position:start], tags)
                text.insert("end", value[start:end], tags + ("hit",))
                position = end
            text.insert("end", value[position:], tags)

        def show_message(title, subtitle=""):
            def fill():
                text.delete("1.0", "end")
                text.insert("end", title + "\n", "empty")
                if subtitle:
                    text.insert("end", subtitle + "\n", "empty_sub")
            write(fill)
            more_button.pack_forget()

        def set_active(index):
            text.tag_remove("active", "1.0", "end")
            state["active"] = index
            if index is None:
                return
            tag = f"r{index}"
            ranges = text.tag_ranges(tag)
            if ranges:
                text.tag_add("active", ranges[0], ranges[-1])
                text.see(ranges[-1])
                text.see(ranges[0])

        def hover(tag):
            ranges = text.tag_ranges(tag)
            text.tag_remove("hover", "1.0", "end")
            if ranges:
                text.tag_add("hover", ranges[0], ranges[-1])
            text.configure(cursor="hand2")

        def unhover(_event=None):
            text.tag_remove("hover", "1.0", "end")
            text.configure(cursor="arrow")

        def add_result(session_id, message_id, title_line, meta_parts, excerpt=None):
            index = len(state["results"])
            state["results"].append((session_id, message_id))
            tag = f"r{index}"
            start = text.index("end-1c")
            insert_marked(title_line, nova_search.match_spans(title_line, state["terms"]), ("title", tag))
            for part, extra in meta_parts:
                text.insert("end", part, ("title", "meta", tag) + extra)
            text.insert("end", "\n", ("title", tag))
            if excerpt is not None:
                snippet, spans = excerpt
                insert_marked(snippet or "(empty message)", spans, ("excerpt", tag))
                text.insert("end", "\n", ("excerpt", tag))
            else:
                text.insert("end", "\n", ("excerpt", tag))
            text.tag_add(tag, start, "end-1c")
            text.tag_bind(tag, "<Enter>", lambda _e, t=tag: hover(t))
            text.tag_bind(tag, "<Leave>", unhover)
            text.tag_bind(tag, "<Button-1>", lambda _e, i=index: open_result(i))

        def render(page, append):
            if not append:
                state.update(results=[], messages=0, sessions=set(), titles=0, active=None)

            def fill():
                if not append:
                    text.delete("1.0", "end")
                    if page.titles:
                        text.insert("end", "CONVERSATIONS\n", "section")
                        for hit in page.titles:
                            count = f"{hit.message_count} message{'s' if hit.message_count != 1 else ''}"
                            add_result(hit.session_id, None, hit.title,
                                       [(f"   ·   {count}   ·   updated {format_timestamp(hit.updated_at)}", ())])
                    if page.messages:
                        text.insert("end", "MESSAGES\n", "section")
                for hit in page.messages:
                    role, extra = ("You", ()) if hit.role == "user" else ("Nova", ("nova",))
                    add_result(hit.session_id, hit.message_id, fit_text(hit.title, f.ui(14), 420),
                               [("   ·   ", ()), (role, extra), (f"   ·   {format_timestamp(hit.created_at)}", ())],
                               nova_search.make_excerpt(hit.content, state["terms"]))
            write(fill)

            state["titles"] += len(page.titles)
            state["messages"] += len(page.messages)
            state["sessions"].update(hit.session_id for hit in page.messages)
            state["next_before"] = page.next_before_id
            if page.has_more:
                more_button.pack(side="left")
                more_button.configure(state="normal", text="Load more")
            else:
                more_button.pack_forget()

            if not state["results"]:
                status.configure(text="", text_color=MUTED)
                show_message(f"No results for “{state['query']}”",
                             "Try fewer words, a shorter word, or check the spelling.")
                return
            parts = []
            if state["titles"]:
                parts.append(f"{state['titles']} conversation title{'s' if state['titles'] != 1 else ''}")
            if state["messages"]:
                more = "+" if page.has_more else ""
                n_sessions = len(state["sessions"])
                parts.append(f"{state['messages']}{more} message{'s' if state['messages'] != 1 else ''} "
                             f"in {n_sessions} conversation{'s' if n_sessions != 1 else ''}")
            status.configure(text="  ·  ".join(parts) + "  —  newest first", text_color=MUTED)
            if not append:
                text.yview_moveto(0)

        def run_search(append=False):
            if state["pending"]:
                win.after_cancel(state["pending"])
                state["pending"] = None
            query = entry.get().strip()
            if not append:
                state["query"] = query
            terms = nova_search.parse_query(state["query"])
            state["generation"] += 1
            generation = state["generation"]
            if not terms:
                state.update(terms=[], results=[], active=None)
                status.configure(text="", text_color=MUTED)
                show_message("Search all your conversations",
                             "Type a word or two — for example “python error” or “recipe”.")
                return
            state["terms"] = terms
            if state.get("inflight"):
                # One search thread at a time: rerun with the newest query when this one ends.
                state["queued"] = True
                return
            state["inflight"] = True
            query_text = state["query"]
            before = state["next_before"] if append else None
            if append:
                more_button.configure(state="disabled", text="Loading…")
            else:
                status.configure(text="Searching…", text_color=MUTED)

            def worker():
                started = time.perf_counter()
                try:
                    page, error = search_conversations(query_text, before_id=before), None
                except sqlite3.Error as exc:
                    log.error("Search failed: %s", exc)
                    page, error = None, f"Search failed because the database couldn't be read: {exc}"
                except Exception as exc:
                    log.exception("Search failed")
                    page, error = None, f"Search failed: {exc}"
                nova_perf.record("search (one page of results)", (time.perf_counter() - started) * 1000)

                def done():
                    state["inflight"] = False
                    if state.pop("queued", False):
                        run_search()  # the query changed while this ran
                        return
                    if generation != state["generation"]:
                        return  # a newer search has started; drop this one
                    if error:
                        status.configure(text=error, text_color=DANGER)
                        more_button.configure(state="normal", text="Load more")
                        if not append:
                            show_message("Search isn't available right now", "Details are in nova.log.")
                        return
                    render(page, append)
                ui_queue.put(done)

            threading.Thread(target=worker, daemon=True, name="NovaSearch").start()

        def schedule_search(_event=None):
            if entry.get().strip() == (state["query"] or ""):
                return  # cursor keys etc. don't change the query
            if state["pending"]:
                win.after_cancel(state["pending"])
            state["pending"] = win.after(250, run_search)

        def open_result(index):
            session_id, message_id = state["results"][index]
            self._last_search_query = entry.get()
            problem = self.open_search_result(session_id, message_id, state["terms"])
            if problem:
                status.configure(text=problem, text_color=WARNING)
                return
            win.destroy()

        def on_return(_event=None):
            if state["pending"] or entry.get().strip() != (state["query"] or ""):
                run_search()
            elif state["active"] is not None:
                open_result(state["active"])
            elif state["results"]:
                open_result(0)
            return "break"

        def move(step):
            if not state["results"]:
                return "break"
            current = state["active"]
            index = 0 if current is None else max(0, min(len(state["results"]) - 1, current + step))
            set_active(index)
            return "break"

        def clear_query():
            entry.delete(0, "end")
            run_search()
            entry.focus_set()

        def on_escape(_event=None):
            if entry.get():
                clear_query()
            else:
                win.destroy()
            return "break"

        entry.bind("<KeyRelease>", schedule_search, add="+")
        entry.bind("<Return>", on_return)
        entry.bind("<Down>", lambda _e: move(1))
        entry.bind("<Up>", lambda _e: move(-1))
        win.bind("<Escape>", on_escape)

        pump()
        previous = getattr(self, "_last_search_query", "")
        if previous:
            entry.insert(0, previous)
        run_search()
        win.after(120, lambda: (entry.focus_set(), entry.select_range(0, "end")))

    # -------------------- Documents --------------------
    def _answer_from_documents(self, session_id, text, question, speak):
        """Answer from indexed documents. Runs on a worker thread.

        Only retrieved passages are sent to the local model; if nothing relevant
        is found, Nova says so without asking the model, so it can't guess.
        """
        self.events.put(("assistant_start", None))
        self.events.put(("doc_reply_started", None))
        self.events.put(("status", "Searching your documents…"))
        try:
            save_message_safe(session_id, "user", text)
            if not question:
                answer = spoken = nova_documents.USAGE_MESSAGE
                self.events.put(("token", answer))
            else:
                con = get_db_connection()
                try:
                    project_id = nova_context.session_project(con, session_id)
                    result = nova_documents.retrieve(con, question, project_id=project_id)
                finally:
                    con.close()
                if not result.passages:
                    answer = spoken = nova_documents.no_answer_message(result)
                    self.events.put(("token", answer))
                else:
                    count = len({p.name for p in result.passages})
                    self.events.put(("status", f"Reading {len(result.passages)} passages from {count} "
                                               f"document{'s' if count != 1 else ''}…"))
                    parts, first, final, started = [], None, None, time.perf_counter()
                    cancel = _cancel_event(self)
                    stream = ollama.chat(
                        model=active_model(),
                        messages=nova_documents.build_messages(question, result.passages),
                        stream=True,
                        options={"num_ctx": 4096, "temperature": 0.2},
                    )
                    chunks = cancellable_stream(stream, cancel)
                    try:
                        for chunk in chunks:
                            if cancel.is_set():
                                break
                            piece = _stream_piece(chunk)
                            if piece:
                                if first is None:
                                    first = time.perf_counter() - started
                                    self.events.put(("reply_started", None))
                                parts.append(piece)
                                self.events.put(("token", piece))
                            if _chunk_done(chunk):
                                final = chunk
                    finally:
                        chunks.close()
                    if cancel.is_set() and final is None:
                        parts.append("\n\n_(Stopped.)_")
                        self.events.put(("token", "\n\n_(Stopped.)_"))
                    record_reply_timings("document answer", started, first, final)
                    spoken = "".join(parts).strip()
                    if not spoken:
                        spoken = "I couldn't generate an answer from the excerpts. Please try again."
                        self.events.put(("token", spoken))
                    footer = nova_documents.format_sources(result, spoken)
                    if self.indexer.busy:
                        footer += "\n\n_Indexing is still running, so recently added files may not be included yet._"
                    self.events.put(("token", "\n\n" + footer))
                    answer = spoken + "\n\n" + footer
                    log.info("Document answer from %d passages (%s)", len(result.passages), result.method)
            message_id = save_message_safe(session_id, "assistant", answer)
            if question:
                try:   # remembered for follow-up questions and “What Nova used”
                    with_db(nova_context.log_document_answer, session_id, message_id, text, result,
                            with_db(nova_context.get_project, project_id))
                    if result.passages:
                        count = len(result.passages)
                        self.events.put(("context_link", (session_id, message_id,
                                                          f"Used {count} document excerpt{'s' if count != 1 else ''}")))
                except Exception as exc:
                    log.warning("Couldn't record the document answer's context: %s", exc)
            if speak:
                try:
                    speak_text(spoken)  # the answer, without the source list
                except Exception as exc:
                    log.warning("Speech output failed: %s", exc)
            self.events.put(("generation_done", None))
        except sqlite3.Error as exc:
            log.error("Document search failed: %s", exc)
            self.events.put(("generation_error", f"Couldn't search your documents: {exc}"))
        except Exception as exc:
            log.error("Document answer failed: %s", exc)
            self.events.put(("generation_error", nova_diagnostics.describe_ollama_error(exc, active_model())))

    def open_documents(self):
        """Documents window: add/refresh/reindex/remove sources and ask questions."""
        existing = getattr(self, "_documents_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

        f = self.fonts
        win = self._dialog("Documents", "880x700", (720, 560))
        self._documents_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(
            body, "Documents",
            "Add files or folders, then ask Nova about them. Documents are read on this PC and never "
            "uploaded, and Nova never changes or deletes them. Sources added while a project is open belong to "
            "that project; change it with the menu on each source.",
        )

        ask_bar = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        ask_bar.pack(fill="x")
        question_entry = self._entry(ask_bar, "Ask a question about your documents…", font_size=15,
                                     fg_color=SURFACE, border_width=0)
        question_entry.pack(side="left", fill="x", expand=True, padx=(14, 6), pady=6)

        tools = ctk.CTkFrame(body, fg_color="transparent")
        tools.pack(fill="x", pady=(14, 6))
        status = ctk.CTkLabel(tools, text=self.doc_status, font=f.ui(12), text_color=MUTED, anchor="e")

        ctk.CTkLabel(
            body,
            text=("Tip: in any chat, start a message with “docs:” to ask about your documents, e.g. "
                  "“docs: what is the leave policy?”. Supported: " + nova_documents.SUPPORTED_LABEL
                  + ". Scanned PDFs need OCR first."),
            font=f.ui(12), text_color=SUBTLE, anchor="w", justify="left", wraplength=800,
        ).pack(side="bottom", fill="x", pady=(10, 0))
        listing = self._scroll_list(body)
        listing.pack(fill="both", expand=True, pady=(4, 0))

        chip_style = {
            "indexed": ("#1d3326", SUCCESS), "pending": ("#3a3220", WARNING), "duplicate": (ACCENT_SOFT, ACCENT),
            "error": (DANGER_SOFT, DANGER), "missing": (DANGER_SOFT, DANGER),
            "missing_dependency": (DANGER_SOFT, DANGER),
        }

        def set_status(text):
            status.configure(text=text, text_color=WARNING if self.indexer.busy else MUTED)

        def chip_for(source):
            counts = source["counts"]
            if source["kind"] == "file":
                state = next(iter(counts), "pending")
                return nova_documents.STATUS_LABELS.get(state, state), chip_style.get(state, ("#3a3220", WARNING))
            if counts.get("pending"):
                return "Indexing", chip_style["pending"]
            if source["scan_note"] and not source["files"]:
                return "Missing", chip_style["missing"]
            problems = sum(n for s, n in counts.items() if s not in ("indexed", "duplicate", "pending"))
            if problems:
                return f"{problems} need attention", ("#3a3220", WARNING)
            return ("Indexed", chip_style["indexed"]) if source["files"] else ("Empty", ("#3a3220", WARNING))

        projects_now = []

        def render_source(source):
            folder = source["kind"] == "folder"
            card = ctk.CTkFrame(listing, fg_color=SURFACE, corner_radius=12)
            card.pack(fill="x", pady=4, padx=(0, 8))
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(10, 0))
            ctk.CTkLabel(top, text=Icon.FOLDER if folder else Icon.DOCUMENT, font=f.icon(15), text_color=MUTED,
                         width=22).pack(side="left")
            title_font = f.ui(14, semibold=True)
            ctk.CTkLabel(top, text=fit_text(source["name"], title_font, 360), font=title_font, text_color=TEXT,
                         anchor="w").pack(side="left", padx=(6, 10))
            label, (chip_fg, chip_text) = chip_for(source)
            self._chip(top, label, chip_fg, chip_text).pack(side="left")
            remove = icon_button(top, f, Icon.DELETE, lambda s=source: remove_source(s), size=30, icon_size=12,
                                 hover=DANGER_SOFT, text_color=SUBTLE)
            remove.pack(side="right")
            Tooltip(remove, "Remove from index (the files stay on your PC)", f)
            again = icon_button(top, f, Icon.REFRESH, lambda s=source: reindex_source(s), size=30, icon_size=13,
                                text_color=SUBTLE)
            again.pack(side="right", padx=2)
            Tooltip(again, "Reindex (read again from scratch)", f)
            # Project: which chats may use this source (No project: every chat).
            choices = {"No project": None}
            for project in projects_now:
                choices[project.name] = project.id
            project_var = tk.StringVar(value=next((name for name, pid in choices.items()
                                                   if pid == source.get("project_id")), "No project"))

            def set_project(label, s=source, options=choices):
                try:
                    with_db(nova_context.set_source_project, s["id"], options.get(label))
                except (sqlite3.Error, nova_context.ProjectError) as exc:
                    messagebox.showerror("Documents", f"Couldn't change the project: {exc}", parent=win)
                    return
                status.configure(text=f"{s['name']}: " + (f"used only in “{label}” chats" if options.get(label)
                                                          else "used in every chat"), text_color=SUCCESS)

            menu = ctk.CTkOptionMenu(
                top, variable=project_var, values=list(choices), command=set_project, width=150, height=28,
                corner_radius=8, dynamic_resizing=False, font=f.ui(12), dropdown_font=f.ui(12), fg_color=SURFACE_2,
                button_color=SURFACE_2, button_hover_color=BORDER, text_color=MUTED, dropdown_fg_color=SURFACE,
                dropdown_hover_color=SURFACE_2, dropdown_text_color=TEXT)
            menu.pack(side="right", padx=(0, 8))
            Tooltip(menu, "Project: which chats may use this source (No project: every chat)", f)

            info = ctk.CTkFrame(card, fg_color="transparent")
            info.pack(fill="x", padx=(44, 14), pady=(2, 10))
            ctk.CTkLabel(info, text=fit_text(source["path"], f.ui(12), 700), font=f.ui(12), text_color=SUBTLE,
                         anchor="w").pack(fill="x")
            passages = f"{source['chunks']} passage{'s' if source['chunks'] != 1 else ''}"
            if folder:
                indexed = source["counts"].get("indexed", 0)
                summary = (f"{source['files']} file{'s' if source['files'] != 1 else ''} · "
                           f"{indexed} indexed · {passages}")
                if source["last_scan_at"]:
                    summary += f" · checked {format_timestamp(source['last_scan_at'])}"
            else:
                summary = f"{source['file_type'] or 'File'} · {passages}"
                if source["indexed_at"]:
                    summary += f" · read {format_timestamp(source['indexed_at'])}"
            ctk.CTkLabel(info, text=summary, font=f.ui(12), text_color=MUTED, anchor="w").pack(fill="x", pady=(2, 0))
            notes = []
            if source["scan_note"]:
                notes.append((source["scan_note"], WARNING))
            if not folder and source["detail"]:
                state = next(iter(source["counts"]), "")
                notes.append((source["detail"], MUTED if state == "indexed" else WARNING))
            if folder:
                for name, state, detail in source["problems"]:
                    label_text = nova_documents.STATUS_LABELS.get(state, state)
                    notes.append((f"{name} — {label_text}: {detail}" if detail else f"{name} — {label_text}",
                                  ACCENT if state == "duplicate" else WARNING))
                hidden = (sum(n for s, n in source["counts"].items() if s not in ("indexed", "pending"))
                          - len(source["problems"]))
                if hidden > 0:
                    notes.append((f"…and {hidden} more.", SUBTLE))
            for text, color in notes:
                ctk.CTkLabel(info, text=text, font=f.ui(12), text_color=color, anchor="w", justify="left",
                             wraplength=720).pack(fill="x", pady=(2, 0))

        def refresh_list():
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            for child in listing.winfo_children():
                child.destroy()
            try:
                con = get_db_connection()
                try:
                    sources = nova_documents.list_sources(con)
                finally:
                    con.close()
            except sqlite3.Error as exc:
                log.error("Couldn't load the document index: %s", exc)
                self._empty_state(listing, Icon.WARNING, "Couldn't load the document index", str(exc))
                return
            if not sources:
                self._empty_state(listing, Icon.DOCUMENT, "No documents yet",
                                  "Add files or a folder above, then ask Nova questions about them.")
                return
            projects_now[:] = self._projects()
            for source in sources:
                render_source(source)

        def working(message):
            status.configure(text=message, text_color=WARNING)

        def add_files():
            paths = filedialog.askopenfilenames(
                parent=win, title="Add documents to Nova",
                filetypes=[("Documents", " ".join(f"*{ext}" for ext in nova_documents.SUPPORTED_TYPES)),
                           ("All files", "*.*")],
            )
            if paths:
                self.indexer.add_paths(paths, self.project_view if isinstance(self.project_view, int) else None)
                working(f"Indexing {len(paths)} file{'s' if len(paths) != 1 else ''}…")

        def add_folder():
            path = filedialog.askdirectory(parent=win, title="Add a folder to Nova", mustexist=True)
            if not path:
                return
            resolved = nova_documents.normalize_path(path)
            if resolved.parent == resolved or resolved == Path.home():
                if not messagebox.askyesno(
                    "Add folder",
                    f"{resolved} may contain a very large number of files. Nova indexes up to "
                    f"{nova_documents.MAX_FOLDER_FILES:,} supported files per folder.\n\nAdd it anyway?",
                    parent=win,
                ):
                    return
            self.indexer.add_paths([path], self.project_view if isinstance(self.project_view, int) else None)
            working("Indexing folder…")

        def refresh_all():
            self.indexer.refresh()
            working("Checking for changed files…")

        def reindex_source(source):
            self.indexer.reindex(source["id"])
            working(f"Reindexing {source['name']}…")

        def remove_source(source):
            what = "folder and its files" if source["kind"] == "folder" else "file"
            if not messagebox.askyesno(
                "Remove from index",
                f"Remove “{source['name']}” from Nova's index?\n\nIts passages are deleted from the index. "
                f"The {what} on your PC won't be changed or deleted.",
                parent=win,
            ):
                return
            self.indexer.remove(source["id"])
            working(f"Removing {source['name']}…")

        def ask_question(_event=None):
            question = question_entry.get().strip()
            if not question:
                question_entry.focus_set()
                return "break"
            if self.busy:
                working("Wait for Nova to finish the current reply, then ask again.")
                return "break"
            text = f"docs: {question}"
            win.destroy()
            self.show_window()
            self.append_message("user", text)
            self.handle_user_message(text)
            return "break"

        self._button(ask_bar, "Ask", ask_question, width=80).pack(side="right", padx=(6, 10), pady=8)
        question_entry.bind("<Return>", ask_question)
        self._button(tools, "+  Add files", add_files, kind="secondary", width=116).pack(side="left")
        self._button(tools, "+  Add folder", add_folder, kind="secondary", width=124).pack(side="left", padx=8)
        refresh_button = self._button(tools, "Refresh", refresh_all, kind="secondary", width=96)
        refresh_button.pack(side="left")
        Tooltip(refresh_button, "Check every document for changes", f)
        status.pack(side="right", fill="x", expand=True, padx=(12, 0))

        def on_destroy(event):
            if event.widget is win:
                self._documents_ui = None

        win.bind("<Destroy>", on_destroy, add="+")
        self._documents_ui = {"status": set_status, "refresh": refresh_list}
        refresh_list()
        set_status(self.doc_status or ("Indexing…" if self.indexer.busy else ""))
        win.after(120, question_entry.focus_set)

    # -------------------- Multi-step tasks --------------------
    def _finish_task_command(self, session_id, text, reply, speak):
        try:
            save_message_safe(session_id, "user", text)
            save_message_safe(session_id, "assistant", reply)
        except Exception as exc:
            reply += f"\n\n(Note: couldn't save this exchange to chat history: {exc})"
        self.finish_action(reply, speak)
        ui = getattr(self, "_tasks_ui", None)
        if ui:
            ui()

    def _record_task(self, plan, session_id, status):
        try:
            return with_db(nova_tasks.create_run, plan, session_id, status)
        except sqlite3.Error as exc:
            log.error("Couldn't record task: %s", exc)
            return None

    def _review_task(self, session_id, text, plan, speak):
        """Main thread: reject, resolve chosen folders, check duplicates, then show the plan."""
        services = self.task_runner.services
        if not plan.runnable:
            self._record_task(plan, session_id, "rejected")
            problems = "\n".join(f"• {p}" for p in plan.problems) or "• Nothing in the request matched a task step."
            reply = (f"I can't run that as a task, so nothing was done.\n\n{problems}\n\n"
                     f"Tasks can only use these steps:\n{nova_tasks.supported_summary()}")
            log.info("Task rejected (%s planner, %d problem(s))", plan.planner, len(plan.problems))
            self._finish_task_command(session_id, text, reply, speak)
            return
        # Folders and files chosen in a dialog count as allowed for this task only.
        for index, name, kind in plan.needs_pick():
            step = plan.steps[index]
            self.set_status(f"Choose a {kind} for the task")
            title = f"Step {index + 1}: choose the {kind} — {step.description}"
            if kind == "folder":
                chosen = filedialog.askdirectory(parent=self.root, mustexist=True, title=title)
            else:
                chosen = filedialog.askopenfilename(parent=self.root, title=title)
            if not chosen:
                self._record_task(plan, session_id, "cancelled")
                self._finish_task_command(session_id, text, f"Task cancelled: no {kind} was chosen.", speak)
                return
            try:
                if kind == "folder" and step.action in ("open_folder", "open_folder_in_vscode", "search_files"):
                    resolved = str(nova_tasks.check_folder_path(chosen))
                elif kind == "folder":
                    resolved = str(nova_actions.checked_folder(chosen, services))
                else:
                    resolved = str(nova_actions.checked_file(chosen, services))
            except nova_tasks.PlanError as exc:
                self._record_task(plan, session_id, "cancelled")
                self._finish_task_command(session_id, text, f"Task cancelled: {exc}", speak)
                return
            step.params[name] = resolved
            plan.picked.add(resolved)
        nova_tasks.validate_plan(plan, services, allow_ask=True)
        if not plan.runnable:
            self._review_task(session_id, text, plan, speak)
            return
        # Folders the task needs that aren't allowed yet: ask, for this task only.
        for folder in plan.needs_approval():
            changes = any(folder in s.unapproved and nova_tasks.REGISTRY[s.action].permission != nova_tasks.READ
                          for s in plan.steps)
            what = "create, copy, move, rename or recycle files" if changes else "open or read files"
            self.set_status("Nova is asking about a folder")
            if not messagebox.askyesno(
                    "Allow this folder?",
                    f"This task needs to {what} in:\n\n{folder}\n\nIt isn't one of your allowed folders. Allow it "
                    "for this task only?\n\n(To allow a folder permanently, add it in Tasks › Allowed folders.)",
                    icon="warning", default="no", parent=self.root):
                self._record_task(plan, session_id, "cancelled")
                self._finish_task_command(session_id, text, f"Task cancelled: {folder} wasn't allowed, so nothing "
                                                            "was done.", speak)
                return
            plan.picked.add(folder)
        nova_tasks.validate_plan(plan, services, allow_ask=False)
        if not plan.runnable:
            self._review_task(session_id, text, plan, speak)
            return
        if plan.auto_run:            # e.g. “switch to notepad”: nothing to review
            self._run_task_now(session_id, text, plan, speak)
            return
        try:
            seconds = with_db(nova_tasks.recent_duplicate, plan)
        except sqlite3.Error:
            seconds = None
        if seconds is not None and not messagebox.askyesno(
            "Run again?", f"You ran this exact task {seconds} seconds ago. Run it again?", parent=self.root,
        ):
            self._record_task(plan, session_id, "cancelled")
            self._finish_task_command(session_id, text, "Okay, I didn't run that task again.", speak)
            return
        self._open_task_dialog(session_id, text, plan, speak)

    def _run_task_now(self, session_id, text, plan, speak):
        """A single harmless step (plan.auto_run): run it without the review window, still recorded."""
        run_id = self._record_task(plan, session_id, "planned")
        if run_id is None:
            self._finish_task_command(session_id, text, "That couldn't be recorded, so it wasn't done.", speak)
            return

        def worker():
            try:
                status, _summary = self.task_runner.run(run_id, plan, threading.Event(), lambda step: False)
                step = plan.steps[0]
                summary = step.result if status == "succeeded" else (step.error or f"That didn't work ({status}).")
            except nova_tasks.AlreadyRunning as exc:
                status, summary = "failed", f"That wasn't done: {exc}"
            except Exception as exc:
                log.exception("Task %s crashed", run_id)
                status, summary = "failed", f"That stopped because of an unexpected error: {exc}"
            self.events.put(("task_done", (session_id, text, run_id, status, summary, speak)))

        threading.Thread(target=worker, daemon=True, name="NovaTask").start()

    def _index_document_for_task(self, path, should_stop):
        """read_document step: index now, in the open chat's project, and refresh Documents."""
        try:
            return index_document_now(path, should_stop, self._chat_project_id())
        finally:
            self.events.put(("doc_changed", None))

    def _open_task_dialog(self, session_id, text, plan, speak):
        f = self.fonts
        win = self._dialog("Task", "680x600", (560, 460))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        planner_note = (
            "Nova read your request as these steps." if plan.planner == "rules" else
            "The AI model proposed these steps from your request. Check them: nothing runs until you click Run."
        )
        self._dialog_header(body, "Review this task", planner_note)
        ctk.CTkLabel(body, text=f"“{plan.request}”", font=f.ui(13), text_color=MUTED, anchor="w", justify="left",
                     wraplength=600).pack(fill="x", pady=(0, 10))
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        note = ctk.CTkLabel(body, text="Steps run in order. If one fails, Nova stops and skips the rest.",
                            font=f.ui(12), text_color=SUBTLE, anchor="w", justify="left", wraplength=600)
        note.pack(side="bottom", fill="x", pady=(8, 0))
        rows_frame = self._scroll_list(body)
        rows_frame.pack(fill="both", expand=True)

        colors = {"pending": SUBTLE, "running": WARNING, "succeeded": SUCCESS, "failed": DANGER,
                  "timed_out": DANGER, "skipped": SUBTLE, "cancelled": SUBTLE, "declined": WARNING}
        rows = []
        for number, step in enumerate(plan.steps, start=1):
            card = ctk.CTkFrame(rows_frame, fg_color=SURFACE, corner_radius=12)
            card.pack(fill="x", pady=3, padx=(0, 8))
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=10)
            icon = ctk.CTkLabel(top, text=nova_tasks.STEP_ICONS["pending"], width=22, font=f.ui(15, semibold=True),
                                text_color=SUBTLE)
            icon.pack(side="left")
            ctk.CTkLabel(top, text=f"{number}. {step.description}", font=f.ui(14), text_color=TEXT, anchor="w",
                         justify="left", wraplength=520).pack(side="left", fill="x", expand=True, padx=(4, 0))
            if getattr(step, "preview", None):          # e.g. the text to copy (shown here, never stored)
                ctk.CTkLabel(card, text=f"“{step.preview}”", font=f.ui(12), text_color=MUTED, anchor="w",
                             justify="left", wraplength=560).pack(fill="x", padx=(40, 14), pady=(0, 6))
            permission = getattr(nova_tasks.REGISTRY.get(step.action), "permission", nova_tasks.NONE)
            note_text = (f"Nova will ask again before this step: it {step.sensitive}." if step.sensitive else
                         ("Changes files: " + nova_tasks.PERMISSION_LABELS[permission] + "."
                          if permission in (nova_tasks.CHANGE, nova_tasks.DESTRUCTIVE) else ""))
            detail = ctk.CTkLabel(card, text=note_text, font=f.ui(12),
                                  text_color=WARNING if step.sensitive else MUTED, anchor="w",
                                  justify="left", wraplength=560)
            if note_text:
                detail.pack(fill="x", padx=(40, 14), pady=(0, 10))
            rows.append((icon, detail))

        state = {"run_id": None, "cancel": threading.Event(), "finished": False}

        def update(position, status, message):
            if position >= len(rows):
                return
            icon, detail = rows[position]
            try:
                icon.configure(text=nova_tasks.STEP_ICONS.get(status, "·"), text_color=colors.get(status, SUBTLE))
                if message:
                    detail.configure(text=message, text_color=DANGER if status in ("failed", "timed_out")
                                     else (MUTED if status != "declined" else WARNING))
                    if not detail.winfo_ismapped():
                        detail.pack(fill="x", padx=(40, 14), pady=(0, 10))
            except tk.TclError:
                pass

        def done(status, summary):
            state["finished"] = True
            try:
                heading = {"succeeded": ("All steps succeeded.", SUCCESS), "cancelled": ("Task cancelled.", WARNING)}
                text_, color = heading.get(status, ("The task stopped before finishing. See the steps above.", DANGER))
                note.configure(text=text_, text_color=color)
                run_button.configure(text="Close", state="normal", command=win.destroy)
                cancel_button.pack_forget()
            except tk.TclError:
                pass

        def cancel_before_run():
            self._record_task(plan, session_id, "cancelled")
            self._task_ui = None
            win.destroy()
            self._finish_task_command(session_id, text, "Okay, I didn't run that task.", speak)

        def stop_running():
            state["cancel"].set()
            note.configure(text="Stopping after the current step…", text_color=WARNING)
            cancel_button.configure(state="disabled")

        def start():
            run_button.configure(state="disabled", text="Running…")
            cancel_button.configure(text="Stop", command=stop_running)
            run_id = self._record_task(plan, session_id, "planned")
            if run_id is None:
                done("failed", "")
                self._finish_task_command(session_id, text, "The task couldn't be recorded, so it wasn't run.", speak)
                return
            state["run_id"] = run_id
            self.set_status("Running task…")
            log.info("Task %s started (%d steps, %s)", run_id, len(plan.steps), plan.planner)

            def confirm(step):
                answer = queue.Queue()
                self.events.put(("task_confirm", (step, answer)))
                try:
                    return answer.get(timeout=nova_tasks.CONFIRM_TIMEOUT)
                except queue.Empty:
                    return False

            def worker():
                try:
                    status, summary = self.task_runner.run(run_id, plan, state["cancel"], confirm)
                except nova_tasks.AlreadyRunning as exc:
                    status, summary = "failed", f"The task wasn't run: {exc}"
                except Exception as exc:
                    log.exception("Task %s crashed", run_id)
                    status, summary = "failed", f"The task stopped because of an unexpected error: {exc}"
                log.info("Task %s finished: %s", run_id, status)
                self.events.put(("task_done", (session_id, text, run_id, status, summary, speak)))

            threading.Thread(target=worker, daemon=True, name="NovaTask").start()

        def on_close():
            if state["run_id"] is None and not state["finished"]:
                cancel_before_run()
            elif not state["finished"]:
                stop_running()  # keep running the current step, then stop
                win.destroy()
            else:
                win.destroy()

        run_button = self._button(footer, "Run task", start, width=120)
        run_button.pack(side="right")
        cancel_button = self._button(footer, "Cancel", cancel_before_run, kind="secondary", width=100)
        cancel_button.pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", on_close)
        self._task_ui = {"state": state, "update": update, "done": done, "win": win}
        self.set_status("Review the task")
        win.after(150, run_button.focus_set)

    def _handle_task_event(self, event, payload):
        ui = getattr(self, "_task_ui", None)
        if event == "task_update":
            run_id, position, status, message = payload
            if ui and ui["state"]["run_id"] == run_id:
                ui["update"](position, status, message)
            if status == "running":
                self.set_status(f"Task step {position + 1} running…")
        elif event == "task_confirm":
            step, answer = payload
            parent = ui["win"] if ui and ui["win"].winfo_exists() else self.root
            permission = getattr(nova_tasks.REGISTRY.get(step.action), "permission", nova_tasks.NONE)
            risky = permission in (nova_tasks.DESTRUCTIVE, nova_tasks.CLIPBOARD, nova_tasks.CHANGE)
            answer.put(messagebox.askyesno(
                "Confirm step",
                f"Run this step?\n\n{step.description}"
                + (f"\n\n“{step.preview}”" if getattr(step, "preview", None) else "")
                + f"\n\nNova is asking because this step {step.sensitive}."
                + ("\n\nNothing has been changed yet." if risky else ""),
                icon="warning", default="no" if risky else "yes", parent=parent,
            ))
        elif event == "task_done":
            session_id, text, run_id, status, summary, speak = payload
            if ui and ui["state"]["run_id"] == run_id:
                try:
                    ui["done"](status, summary)
                except tk.TclError:
                    pass
                self._task_ui = None
            self._finish_task_command(session_id, text, summary, speak)

    def _allowed_folders_section(self, parent, win):
        """Tasks › Allowed folders: where tasks may create, copy, move, rename or recycle files."""
        f = self.fonts
        box = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=12)
        box.pack(fill="x", pady=(4, 2))
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(head, text="Allowed folders", font=f.ui(14, semibold=True), text_color=TEXT,
                     anchor="w").pack(side="left")
        listing = ctk.CTkFrame(box, fg_color="transparent")
        ctk.CTkLabel(box, text="Tasks may create, copy, move, rename or recycle files only here (and in a folder you "
                               "choose or allow for one task). Files in Downloads, Documents, Desktop and Pictures "
                               "can also be opened or read. Windows, program, app-data and browser folders are always "
                               "off limits.", font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                     wraplength=820).pack(fill="x", padx=14)
        listing.pack(fill="x", padx=14, pady=(4, 10))
        message = ctk.CTkLabel(box, text="", font=f.ui(12), text_color=DANGER, anchor="w")

        def save(folders):
            self._persist_setting("approved_folders", list(folders))
            render()

        def add():
            chosen = filedialog.askdirectory(parent=win, mustexist=True, title="Allow Nova's tasks to change files in…")
            if not chosen:
                return
            try:
                folder = nova_actions.checked_folder(chosen, self.task_runner.services)
                if folder.parent == folder:
                    raise nova_tasks.PlanError("A whole drive can't be allowed. Choose a folder on it.")
            except nova_tasks.PlanError as exc:
                message.configure(text=str(exc))
                message.pack(fill="x", padx=14, pady=(0, 8))
                return
            message.pack_forget()
            current = list(self.settings.get("approved_folders") or [])
            if str(folder).lower() not in (c.lower() for c in current):
                save(current + [str(folder)])

        def remove(folder):
            save([c for c in (self.settings.get("approved_folders") or []) if c != folder])

        def render():
            for child in listing.winfo_children():
                child.destroy()
            folders = list(self.settings.get("approved_folders") or [])
            if not folders:
                ctk.CTkLabel(listing, text="None yet. Nova asks before using any other folder.", font=f.ui(12),
                             text_color=SUBTLE, anchor="w").pack(fill="x")
            for folder in folders:
                row = ctk.CTkFrame(listing, fg_color="transparent")
                row.pack(fill="x", pady=1)
                ctk.CTkLabel(row, text=folder, font=f.ui(13), text_color=TEXT, anchor="w").pack(side="left")
                ctk.CTkButton(row, text="Remove", width=70, height=26, corner_radius=8, fg_color=SURFACE_2,
                              hover_color=BORDER, text_color=MUTED, font=f.ui(12),
                              command=lambda c=folder: remove(c)).pack(side="right")

        ctk.CTkButton(head, text="Add folder…", width=100, height=28, corner_radius=8, fg_color=SURFACE_2,
                      hover_color=BORDER, text_color=TEXT, font=f.ui(12), command=add).pack(side="right")
        render()

    def open_tasks(self):
        """Task history, what tasks can do, and a box to start one."""
        existing = getattr(self, "_tasks_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass
        f = self.fonts
        win = self._dialog("Tasks", "900x720", (760, 560))
        self._tasks_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(
            body, "Tasks",
            "Ask Nova to do things on this PC, e.g. “open my downloads folder and search it for invoice” or “copy a "
            "file I choose to a folder I choose”. You see the steps and the paths before anything runs, and Nova asks "
            "again before moving, renaming, replacing or recycling a file. Tasks can only use the steps listed below: "
            "they never run commands or programs, change settings, or touch passwords, browser data or system "
            "folders.",
        )
        bar = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        bar.pack(fill="x")
        entry = self._entry(bar, "Describe a task…", font_size=15, fg_color=SURFACE, border_width=0)
        entry.pack(side="left", fill="x", expand=True, padx=(14, 6), pady=6)

        def plan_task(_event=None):
            request = entry.get().strip()
            if not request:
                entry.focus_set()
                return "break"
            if self.busy:
                self.set_status("Wait for Nova to finish, then try again")
                return "break"
            message = f"task: {request}"
            win.destroy()
            self.show_window()
            self.append_message("user", message)
            self.handle_user_message(message)
            return "break"

        self._button(bar, "Plan", plan_task, width=80).pack(side="right", padx=(6, 10), pady=8)
        entry.bind("<Return>", plan_task)
        ctk.CTkLabel(body, text="Steps tasks can use:  " + "  ·  ".join(a.title for a in nova_tasks.REGISTRY.values()),
                     font=f.ui(12), text_color=MUTED, anchor="w", justify="left", wraplength=840).pack(
            fill="x", pady=(10, 4))
        self._allowed_folders_section(body, win)
        history = self._scroll_list(body)
        history.pack(fill="both", expand=True, pady=(6, 0))
        chips = {"succeeded": ("#1d3326", SUCCESS), "failed": (DANGER_SOFT, DANGER), "timed_out": (DANGER_SOFT, DANGER),
                 "cancelled": (SURFACE_2, MUTED), "rejected": (SURFACE_2, MUTED), "running": ("#3a3220", WARNING),
                 "planned": ("#3a3220", WARNING), "interrupted": (DANGER_SOFT, DANGER)}

        def refresh():
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            for child in history.winfo_children():
                child.destroy()
            try:
                runs = with_db(nova_tasks.list_runs, 50)
            except sqlite3.Error as exc:
                self._empty_state(history, Icon.WARNING, "Couldn't load task history", str(exc))
                return
            if not runs:
                self._empty_state(history, Icon.TASKS, "No tasks yet",
                                  "Describe one above, or type “task: …” in any chat.")
                return
            for run in runs:
                card = ctk.CTkFrame(history, fg_color=SURFACE, corner_radius=12)
                card.pack(fill="x", pady=4, padx=(0, 8))
                top = ctk.CTkFrame(card, fg_color="transparent")
                top.pack(fill="x", padx=14, pady=(10, 0))
                fg, color = chips.get(run["status"], (SURFACE_2, MUTED))
                self._chip(top, run["status"].replace("_", " ").title(), fg, color).pack(side="left")
                when = format_timestamp(run["finished_at"] or run["started_at"] or run["created_at"])
                source = {"model": "AI-proposed plan", "agent": "Agent (planned and checked by Nova)"}.get(
                    run["planner"], "Nova's planner")
                ctk.CTkLabel(top, text=f"{when} · {source}", font=f.ui(11), text_color=SUBTLE).pack(side="left", padx=8)
                ctk.CTkLabel(card, text=run["request"] or "(empty request)", font=f.ui(14, semibold=True),
                             text_color=TEXT, anchor="w", justify="left", wraplength=780).pack(
                    fill="x", padx=14, pady=(6, 2))
                for step in run["steps"]:
                    icon = nova_tasks.STEP_ICONS.get(step["status"], "·")
                    detail = step["result"] or step["error"] or ""
                    if step.get("attempts") and step["attempts"] > 1:
                        detail += f" (tried {step['attempts']} times)"
                    if step.get("evidence") and step["status"] == "succeeded":
                        detail += f"\n      Checked: {step['evidence']}"
                    line = f"{icon}  {step['position'] + 1}. {step['description'] or step['action']}"
                    ctk.CTkLabel(card, text=line + (f"\n      {detail}" if detail else ""), font=f.ui(12),
                                 text_color=DANGER if step["status"] in ("failed", "timed_out") else MUTED,
                                 anchor="w", justify="left", wraplength=780).pack(fill="x", padx=14)
                if run["status"] == "rejected" and run["summary"]:
                    ctk.CTkLabel(card, text=run["summary"], font=f.ui(12), text_color=MUTED, anchor="w",
                                 justify="left", wraplength=780).pack(fill="x", padx=14)
                ctk.CTkFrame(card, height=8, fg_color="transparent").pack()

        def on_destroy(event):
            if event.widget is win:
                self._tasks_ui = None

        win.bind("<Destroy>", on_destroy, add="+")
        self._tasks_ui = refresh
        refresh()
        win.after(150, entry.focus_set)

    # -------------------- Reminders (polling and alerts) --------------------
    def _finish_reminder_reply(self, session_id, text, handled, speak):
        """Worker thread: store the reply (and any follow-up question) and show it."""
        reply, self._pending_reminder = handled
        try:
            save_message_safe(session_id, "user", text)
            save_message_safe(session_id, "assistant", reply)
        except Exception as exc:
            reply += f"\n\n(Note: couldn't save this exchange to chat history: {exc})"
        self.events.put(("action_done", (reply, speak)))
        self.events.put(("reminders_changed", None))

    def _refresh_reminder_views(self):
        ui = getattr(self, "_reminders_ui", None)
        if ui:
            try:
                ui()
            except tk.TclError:
                self._reminders_ui = None

    def poll_due_reminders(self):
        """Claim due reminders (each alert exactly once) and show them, even from the tray."""
        if self._maintenance:
            self._schedule("reminders", 2000, self.poll_due_reminders)
            return
        try:
            alerts = with_db(nova_reminders.claim_due)
            if alerts:
                missed = [a for a in alerts if a.missed]
                if missed:
                    self.append_message(
                        "system",
                        f"{len(missed)} reminder{'s' if len(missed) != 1 else ''} came due while Nova wasn't "
                        "running or the PC was asleep. They're shown now; one-off reminders you don't complete "
                        "stay under Overdue in Notes & reminders.",
                    )
                for alert in alerts:
                    log.info("Reminder %s alert (missed=%s)", alert.reminder_id, alert.missed)
                    self._show_reminder_alert(alert)
                self._refresh_reminder_views()
        except Exception as exc:
            log.error("Reminder check failed: %s", exc)
            self.set_status(f"Reminder check failed: {exc}")
        finally:
            self._schedule("reminders", 2000, self.poll_due_reminders)

    def _show_reminder_alert(self, alert):
        """A non-blocking, always-on-top alert. Not tied to the main window, so it
        appears even when Nova is hidden in the tray."""
        self._alert_windows = [w for w in self._alert_windows if w.winfo_exists()]
        self.set_status(f"Reminder: {alert.title}")
        hidden = self.root.state() == "withdrawn"
        if hidden and self.tray_icon is not None:
            try:
                self.tray_icon.notify(alert.title, "Nova reminder")
            except Exception as exc:
                log.debug("Tray notification failed: %s", exc)
        try:
            self.root.bell()
        except tk.TclError:
            pass
        if len(self._alert_windows) >= 4:
            self._show_overflow_alert()
            return
        f = self.fonts
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title("Reminder — Nova")
        win.resizable(False, False)
        self._apply_icon(win)
        width, height = 440, 260
        offset = 34 * len(self._alert_windows)
        x = max(0, win.winfo_screenwidth() - width - 48 - offset)
        y = max(0, win.winfo_screenheight() - height - 110 - offset)
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.attributes("-topmost", True)
        win.after(80, lambda: (win.lift(), win.focus_force()))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=22, pady=18)
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=Icon.CLOCK, font=f.icon(15), text_color=ACCENT, width=20).pack(side="left")
        kind = "Missed reminder" if alert.missed else ("Repeating reminder" if alert.rule else "Reminder")
        ctk.CTkLabel(top, text=kind, font=f.ui(12, semibold=True), text_color=WARNING if alert.missed else ACCENT,
                     anchor="w").pack(side="left", padx=(6, 0))
        ctk.CTkLabel(body, text=alert.title, font=f.heading(19), text_color=TEXT, anchor="w", justify="left",
                     wraplength=390).pack(fill="x", pady=(8, 4))
        for line in nova_reminders.alert_lines(alert):
            ctk.CTkLabel(body, text=line, font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=390).pack(fill="x")
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x")

        def act(action, *args, status, **kwargs):
            try:
                with_db(action, *args, **kwargs)
                self.set_status(status)
            except Exception as exc:
                log.error("Reminder action failed: %s", exc)
                messagebox.showerror("Reminder", str(exc), parent=win)
                return
            win.destroy()
            self._refresh_reminder_views()

        occurrence = alert.occurrence if alert.rule else None
        self._button(buttons, "Done", lambda: act(nova_reminders.complete, alert.reminder_id, occurrence,
                                                  status="Reminder done"), width=84).pack(side="left")
        for label, minutes in (("10 min", 10), ("1 hour", 60)):
            self._button(buttons, label, lambda m=minutes: act(
                nova_reminders.snooze, alert.reminder_id, m, occurrence=alert.occurrence,
                status=f"Snoozed for {m} minutes"), kind="secondary", width=80).pack(side="left", padx=(8, 0))
        dismiss = self._button(buttons, "Dismiss", win.destroy, kind="secondary", width=84)
        dismiss.pack(side="right")
        Tooltip(dismiss, "Close (a one-off reminder stays under Overdue)", f)
        self._alert_windows.append(win)

    def _show_overflow_alert(self):
        existing = getattr(self, "_overflow_alert", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        win = ctk.CTkToplevel(self.root, fg_color=BG)
        win.title("Reminders — Nova")
        win.attributes("-topmost", True)
        win.geometry(f"380x150+{max(0, win.winfo_screenwidth() - 430)}+60")
        self._apply_icon(win)
        ctk.CTkLabel(win, text="More reminders are due", font=self.fonts.heading(18), text_color=TEXT).pack(
            pady=(20, 4))
        self._button(win, "Open reminders", lambda: (win.destroy(), self.open_productivity("Reminders")),
                     width=150).pack(pady=10)
        self._overflow_alert = win

    def _reminder_editor(self, parent, reminder=None, on_saved=None):
        """Exact date, time and repeat for a new or existing reminder."""
        f = self.fonts
        editing = reminder is not None
        win = ctk.CTkToplevel(parent, fg_color=BG)
        win.title(("Edit reminder" if editing else "New reminder") + " — Nova")
        win.geometry("560x560")
        win.minsize(500, 520)
        win.transient(parent)
        self._apply_icon(win)
        win.after(80, lambda: (win.lift(), win.focus_force()))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=24, pady=20)
        # Packed first, so Save and Cancel always keep their space, whatever the window size.
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        ctk.CTkLabel(body, text="Edit reminder" if editing else "New reminder", font=f.heading(20),
                     text_color=TEXT, anchor="w").pack(fill="x")

        def label(text):
            ctk.CTkLabel(body, text=text, font=f.ui(13, semibold=True), text_color=MUTED, anchor="w").pack(
                fill="x", pady=(12, 4))

        label("Remind me to")
        title = self._entry(body, "e.g. revise SQL", width=500)
        title.pack(anchor="w")
        default_due = reminder.due if editing else (datetime.now() + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(row, text="Date (start date for repeats)", font=f.ui(13, semibold=True), text_color=MUTED,
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(row, text="Time (24-hour)", font=f.ui(13, semibold=True), text_color=MUTED,
                     anchor="w").grid(row=0, column=1, sticky="w", padx=(16, 0))
        date_entry = self._entry(row, "YYYY-MM-DD", width=200)
        date_entry.grid(row=1, column=0, sticky="w", pady=(4, 0))
        time_entry = self._entry(row, "HH:MM", width=120)
        time_entry.grid(row=1, column=1, sticky="w", padx=(16, 0), pady=(4, 0))
        label("Repeat")
        rule = reminder.rule if editing else None
        kind = (rule or "once").split(":")[0]
        repeat_var = tk.StringVar(value={"once": "Once", "daily": "Daily", "weekly": "Weekly",
                                         "monthly": "Monthly"}[kind])
        ctk.CTkSegmentedButton(
            body, values=["Once", "Daily", "Weekly", "Monthly"], variable=repeat_var, font=f.ui(13),
            fg_color=SURFACE, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=SURFACE, unselected_hover_color=SURFACE_2, text_color=TEXT, height=34,
            command=lambda _v: show_options(),
        ).pack(anchor="w")
        # height=1: empty (Once, Daily) it must collapse, not keep CTkFrame's default 200 px,
        # which pushed Save and Cancel out of the window.
        options = ctk.CTkFrame(body, fg_color="transparent", height=1)
        options.pack(fill="x", pady=(10, 0))
        day_vars = {}
        weekly = ctk.CTkFrame(options, fg_color="transparent")
        chosen = set(rule.split(":")[1].split(",")) if rule and rule.startswith("weekly") else {
            nova_reminders.DAY_CODES[default_due.weekday()]}
        for code, name in zip(nova_reminders.DAY_CODES, nova_reminders.DAY_NAMES):
            day_vars[code] = tk.BooleanVar(value=code in chosen)
            ctk.CTkCheckBox(weekly, text=name[:3], variable=day_vars[code], width=60, font=f.ui(13), text_color=TEXT,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER, checkbox_width=18,
                            checkbox_height=18).pack(side="left")
        monthly = ctk.CTkFrame(options, fg_color="transparent")
        ctk.CTkLabel(monthly, text="Day of the month (1–31, or “last”)", font=f.ui(13), text_color=MUTED).pack(
            side="left")
        month_day = self._entry(monthly, "15", width=80)
        month_day.pack(side="left", padx=(10, 0))
        note = ctk.CTkLabel(body, text="", font=f.ui(12), text_color=SUBTLE, anchor="w", justify="left",
                            wraplength=500)
        note.pack(fill="x", pady=(10, 0))

        def show_options():
            weekly.pack_forget()
            monthly.pack_forget()
            choice = repeat_var.get()
            if choice == "Weekly":
                weekly.pack(anchor="w")
            elif choice == "Monthly":
                monthly.pack(anchor="w")
            note.configure(text={
                "Monthly": "Days past a month's end fall on its last day (the 31st is 30 April, 28/29 February).",
                "Once": "Times use this PC's clock and time zone.",
            }.get(choice, "The time stays the same through daylight-saving changes."))

        title.insert(0, reminder.title if editing else "")
        date_entry.insert(0, default_due.strftime("%Y-%m-%d"))
        time_entry.insert(0, (reminder.anchor if editing and reminder.anchor else default_due.strftime("%H:%M")))
        month_day.insert(0, rule.split(":")[1] if rule and rule.startswith("monthly") else str(default_due.day))
        show_options()

        def save():
            text = " ".join(title.get().split())
            if not text:
                messagebox.showwarning("Reminder", "Say what to remind you about.", parent=win)
                return
            try:
                schedule = nova_reminders.schedule_from_fields(
                    date_entry.get(), time_entry.get(), repeat_var.get().lower(),
                    [c for c, v in day_vars.items() if v.get()], month_day.get())
                duplicate = with_db(nova_reminders.find_duplicate, text, schedule,
                                    reminder.id if editing else None)
                if duplicate and not messagebox.askyesno(
                    "Reminder already exists",
                    f"You already have “{duplicate.title}” ({duplicate.describe()}). Save another one anyway?",
                    parent=win,
                ):
                    return
                if editing:
                    with_db(nova_reminders.update, reminder.id, text, schedule)
                    self.set_status("Reminder updated")
                else:
                    with_db(nova_reminders.create, text, schedule, allow_duplicate=bool(duplicate))
                    self.set_status("Reminder saved")
            except ValueError as exc:
                messagebox.showwarning("Reminder", str(exc), parent=win)
                return
            except sqlite3.Error as exc:
                log.error("Saving reminder failed: %s", exc)
                messagebox.showerror("Reminder", f"Couldn't save the reminder: {exc}", parent=win)
                return
            win.destroy()
            if on_saved:
                on_saved()

        self._button(buttons, "Save", save, width=100).pack(side="right")
        self._button(buttons, "Cancel", win.destroy, kind="secondary", width=100).pack(side="right", padx=8)
        win.bind("<Return>", lambda _e: save())
        win.bind("<Escape>", lambda _e: win.destroy())
        win.after(150, title.focus_set)
        return win

    def _build_reminders_tab(self, win, tab):
        """Agenda view: overdue, by day, repeating and completed, with natural-language add."""
        f = self.fonts
        ctk.CTkLabel(
            tab,
            text=("Reminders alert while Nova is running, including when it's hidden in the tray. They can't "
                  "alert while Nova is closed or Windows is off; anything missed is shown as soon as Nova starts "
                  "again. Turn on Settings › Start with Windows to keep Nova running."),
            font=f.ui(12), text_color=MUTED, anchor="w", justify="left", wraplength=860,
        ).pack(fill="x", padx=4, pady=(8, 4))
        form = ctk.CTkFrame(tab, fg_color=SURFACE, corner_radius=14)
        form.pack(fill="x", pady=(4, 0))
        entry = self._entry(form, "e.g. every Monday at 8 PM to revise SQL  ·  tomorrow at 9 AM to study")
        entry.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=12)
        hint = ctk.CTkLabel(tab, text="Type it the way you'd say it, or use Exact time… to pick a date and repeat.",
                            font=f.ui(12), text_color=SUBTLE, anchor="w", justify="left", wraplength=860)
        hint.pack(fill="x", padx=4, pady=(6, 4))
        listing = self._scroll_list(tab)
        listing.pack(fill="both", expand=True, pady=(4, 0))
        menu = tk.Menu(win, tearoff=False)

        def small(parent, text, command, kind="secondary"):
            button = ctk.CTkButton(
                parent, text=text, command=command, width=10, height=28, corner_radius=8, font=f.ui(12),
                fg_color={"secondary": SURFACE_2, "danger": DANGER_SOFT}[kind],
                hover_color={"secondary": BORDER, "danger": "#4a272c"}[kind],
                text_color={"secondary": TEXT, "danger": DANGER}[kind],
            )
            button.pack(side="right", padx=(6, 0))
            return button

        def act(action, *args, status=None, **kwargs):
            try:
                with_db(action, *args, **kwargs)
            except ValueError as exc:
                messagebox.showwarning("Reminders", str(exc), parent=win)
                return
            except sqlite3.Error as exc:
                log.error("Reminder action failed: %s", exc)
                messagebox.showerror("Reminders", str(exc), parent=win)
                return
            if status:
                self.set_status(status)
            refresh()

        def snooze_menu(button, reminder):
            menu.delete(0, "end")
            tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            menu.add_command(label="10 minutes", command=lambda: act(nova_reminders.snooze, reminder.id, 10,
                                                                      status="Snoozed for 10 minutes"))
            menu.add_command(label="1 hour", command=lambda: act(nova_reminders.snooze, reminder.id, 60,
                                                                  status="Snoozed for 1 hour"))
            menu.add_command(label="Tomorrow at 9:00 AM", command=lambda: act(
                nova_reminders.snooze, reminder.id, until=tomorrow, status="Snoozed until tomorrow"))
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
            menu.grab_release()

        def delete(reminder):
            what = ("this repeating reminder and all its future occurrences" if reminder.recurring
                    else "this reminder")
            if messagebox.askyesno("Delete reminder", f"Delete {what}?\n\n“{reminder.title}”\n\nThis can't be undone.",
                                   icon="warning", parent=win):
                act(nova_reminders.delete, reminder.id, status="Reminder deleted")

        def header(text, color=MUTED):
            ctk.CTkLabel(listing, text=text, font=f.ui(13, semibold=True), text_color=color, anchor="w").pack(
                fill="x", padx=4, pady=(12, 2))

        def card(reminder, section, now):
            box = ctk.CTkFrame(listing, fg_color=SURFACE, corner_radius=12)
            box.pack(fill="x", pady=3, padx=(0, 8))
            done = section == "completed"
            ctk.CTkLabel(box, text=Icon.CHECK if done else Icon.CLOCK, font=f.icon(16),
                         text_color=SUCCESS if done else (DANGER if section == "overdue" else MUTED),
                         width=24).pack(side="left", padx=(16, 10), pady=12)
            text = ctk.CTkFrame(box, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True, pady=8)
            ctk.CTkLabel(text, text=reminder.title, font=f.ui(14, semibold=True),
                         text_color=SUBTLE if done else TEXT, anchor="w", justify="left", wraplength=470).pack(fill="x")
            if done:
                detail = f"Completed {format_timestamp(reminder.completed_at)}" if reminder.completed_at else "Completed"
                if reminder.recurring:
                    detail = f"Ended · was {nova_reminders.describe_rule(reminder.rule, reminder.anchor)}"
            elif section == "overdue":
                detail = f"Was due {nova_reminders.when(reminder.due, now)}"
                if reminder.notified_at:
                    detail += f" · alerted {format_timestamp(reminder.notified_at)}"
            else:
                detail = reminder.describe(now)
                if reminder.due != reminder.occurrence and not reminder.recurring:
                    detail += f" · snoozed (originally {nova_reminders.when(reminder.occurrence, now)})"
            ctk.CTkLabel(text, text=detail, font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=470).pack(fill="x")
            actions = ctk.CTkFrame(box, fg_color="transparent")
            actions.pack(side="right", padx=(6, 12))
            small(actions, "Delete", lambda: delete(reminder), "danger")
            if done:
                return
            if reminder.recurring:
                small(actions, "End", lambda: act(nova_reminders.complete, reminder.id, status="Repeating reminder ended"))
                small(actions, "Edit", lambda: self._reminder_editor(win, reminder, refresh))
                small(actions, "Skip next", lambda: act(nova_reminders.skip_next, reminder.id,
                                                        status="Skipped the next occurrence"))
            else:
                small(actions, "Done", lambda: act(nova_reminders.complete, reminder.id, status="Reminder done"))
                small(actions, "Edit", lambda: self._reminder_editor(win, reminder, refresh))
                snooze = small(actions, "Snooze ▾", lambda: None)
                snooze.configure(command=lambda b=snooze: snooze_menu(b, reminder))

        def refresh():
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            for child in listing.winfo_children():
                child.destroy()
            now = datetime.now()
            try:
                groups = with_db(nova_reminders.grouped, now)
            except sqlite3.Error as exc:
                self._empty_state(listing, Icon.WARNING, "Couldn't load reminders", str(exc))
                return
            if not (groups["overdue"] or groups["days"] or groups["recurring"] or groups["completed"]):
                self._empty_state(listing, Icon.CLOCK, "No reminders",
                                  "Add one above, or say “remind me tomorrow at 9 AM to study” in a chat.")
                return
            if groups["overdue"]:
                header(f"Overdue ({len(groups['overdue'])})", DANGER)
                for reminder in groups["overdue"]:
                    card(reminder, "overdue", now)
            for day in sorted(groups["days"]):
                offset = (day - now.date()).days
                name = {0: "Today", 1: "Tomorrow"}.get(offset, day.strftime("%A"))
                header(f"{name} · {day.day} {day.strftime('%b')}" + (f" {day.year}" if day.year != now.year else ""))
                for reminder in groups["days"][day]:
                    card(reminder, "upcoming", now)
            if groups["recurring"]:
                header(f"Repeating ({len(groups['recurring'])})", ACCENT)
                for reminder in groups["recurring"]:
                    card(reminder, "recurring", now)
            if groups["completed"]:
                header("Completed (most recent)", SUBTLE)
                for reminder in groups["completed"][:15]:
                    card(reminder, "completed", now)

        def add(_event=None):
            text = entry.get().strip()
            if not text:
                entry.focus_set()
                return "break"
            request = text if nova_reminders.is_reminder_request(text) else "remind me " + text
            try:
                result = nova_reminders.parse_reminder(request)
                if result.kind == "ok":
                    saved = with_db(nova_reminders.create, result.title, result.schedule)
                    message = nova_reminders.saved_message(saved).split(" Times use")[0]
                    hint.configure(text=message, text_color=SUCCESS if saved.created else WARNING)
                    if saved.created:
                        entry.delete(0, "end")
                    refresh()
                elif result.kind == "clarify":
                    hint.configure(text=result.message + "  Add that to the text above and press Add again.",
                                   text_color=WARNING)
                else:
                    hint.configure(text=result.message or "That doesn't look like a reminder.", text_color=DANGER)
            except Exception as exc:
                log.error("Adding reminder failed: %s", exc)
                hint.configure(text=f"Couldn't add the reminder: {exc}", text_color=DANGER)
            return "break"

        self._button(form, "Exact time…", lambda: self._reminder_editor(win, None, refresh), kind="secondary",
                     width=110).pack(side="right", padx=(6, 12), pady=12)
        self._button(form, "Add", add, width=80).pack(side="right", padx=(6, 0), pady=12)
        entry.bind("<Return>", add)

        def on_destroy(event):
            if event.widget is win:
                self._reminders_ui = None

        win.bind("<Destroy>", on_destroy, add="+")
        self._reminders_ui = refresh
        refresh()

    # -------------------- Notes & reminders --------------------
    def open_productivity(self, tab=None):
        """Open local Notes and Reminders manager (one window at a time)."""
        existing = getattr(self, "_productivity_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                if tab and getattr(self, "_productivity_tabs", None):
                    self._productivity_tabs.set(tab)
                return
        except tk.TclError:
            pass
        f = self.fonts
        win = self._dialog("Notes & reminders", "960x660", (800, 540))
        self._productivity_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(
            body, "Notes & reminders",
            "Saved on this PC. Reminders pop up at their due time while Nova is running, including from the tray.",
        )
        tabs = ctk.CTkTabview(
            body, fg_color=BG, corner_radius=12, anchor="w",
            segmented_button_fg_color=SURFACE, segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=SURFACE, segmented_button_unselected_hover_color=SURFACE_2,
            text_color=TEXT,
        )
        tabs.pack(fill="both", expand=True)
        notes_tab = tabs.add("Notes")
        reminders_tab = tabs.add("Reminders")
        self._productivity_tabs = tabs
        try:
            tabs._segmented_button.configure(font=f.ui(13, semibold=True), height=34)
        except Exception:
            pass

        # ---- Notes ----
        notes_tab.grid_columnconfigure(1, weight=1)
        notes_tab.grid_rowconfigure(1, weight=1)
        selected = {"id": None}

        left = ctk.CTkFrame(notes_tab, fg_color="transparent", width=290)
        left.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(0, 14), pady=(8, 0))
        left.grid_propagate(False)
        left.pack_propagate(False)
        note_list = self._scroll_list(left)

        editor = ctk.CTkFrame(notes_tab, fg_color=SURFACE, corner_radius=14)
        editor.grid(row=0, column=1, rowspan=2, sticky="nsew", pady=(8, 0))
        title_entry = self._entry(editor, "Title", font_size=18, fg_color=SURFACE, border_width=0, height=46)
        title_entry.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkFrame(editor, height=1, fg_color=BORDER).pack(fill="x", padx=18, pady=(2, 4))
        content_box = ctk.CTkTextbox(
            editor, fg_color=SURFACE, text_color=TEXT, font=f.ui(14), wrap="word",
            border_width=0, scrollbar_button_color=SURFACE_2, scrollbar_button_hover_color=BORDER,
        )
        content_box.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        actions = ctk.CTkFrame(editor, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=(0, 14))
        meta = ctk.CTkLabel(actions, text="New note", font=f.ui(12), text_color=SUBTLE)
        meta.pack(side="left")
        # Project: the note is used as context only in that project's chats (No project: in every chat).
        note_projects = {}
        note_project_var = tk.StringVar(value="No project")
        note_project_menu = ctk.CTkOptionMenu(
            actions, variable=note_project_var, values=["No project"], width=170, height=32, corner_radius=9,
            dynamic_resizing=False, font=f.ui(12), dropdown_font=f.ui(12), fg_color=SURFACE_2,
            button_color=SURFACE_2, button_hover_color=BORDER, text_color=TEXT, dropdown_fg_color=SURFACE,
            dropdown_hover_color=SURFACE_2, dropdown_text_color=TEXT)
        note_project_menu.pack(side="left", padx=(12, 0))
        Tooltip(note_project_menu, "Project: Nova uses this note only in that project's chats "
                                   "(No project: in every chat)", f)

        def project_choices(project_id):
            labels = {"No project": None}
            for project in self._projects():
                labels[project.name] = project.id
            note_projects.clear()
            note_projects.update(labels)
            note_project_menu.configure(values=list(labels))
            note_project_var.set(next((label for label, value in labels.items() if value == project_id), "No project"))

        project_choices(self.project_view if isinstance(self.project_view, int) else None)

        def load_note(note_id):
            row = next((r for r in list_notes_db() if int(r[0]) == note_id), None)
            if row is None:
                return
            selected["id"] = note_id
            title_entry.delete(0, "end")
            title_entry.insert(0, str(row[1]))
            content_box.delete("1.0", "end")
            content_box.insert("1.0", str(row[2] or ""))
            meta.configure(text=f"Last edited {format_timestamp(row[4] or row[3])}")
            project_choices(row[5] if len(row) > 5 else None)
            refresh_notes()

        def refresh_notes():
            for child in note_list.winfo_children():
                child.destroy()
            rows = list_notes_db()
            if not rows:
                self._empty_state(note_list, Icon.NOTE, "No notes yet",
                                  "Write one on the right, or type “note: Title | text” in a chat.")
            card_font = f.ui(14, semibold=True)
            for row in rows:
                note_id = int(row[0])
                active = note_id == selected["id"]
                card = ctk.CTkFrame(note_list, fg_color=SURFACE_2 if active else SURFACE, corner_radius=12)
                card.pack(fill="x", pady=3, padx=(0, 8))
                ctk.CTkLabel(card, text=fit_text(row[1], card_font, 220), font=card_font,
                             text_color=TEXT, anchor="w").pack(fill="x", padx=14, pady=(10, 0))
                preview = " ".join(str(row[2] or "").split()) or "No content"
                ctk.CTkLabel(card, text=fit_text(preview, f.ui(12), 220), font=f.ui(12),
                             text_color=MUTED, anchor="w").pack(fill="x", padx=14)
                project_name = next((name for name, pid in note_projects.items()
                                     if pid is not None and len(row) > 5 and pid == row[5]), None)
                stamp = format_timestamp(row[4] or row[3]) + (f"  ·  {project_name}" if project_name else "")
                ctk.CTkLabel(card, text=stamp, font=f.ui(11),
                             text_color=SUBTLE, anchor="w").pack(fill="x", padx=14, pady=(0, 10))
                bind_tree(card, "<Button-1>", lambda _e, nid=note_id: load_note(nid))
                set_cursor(card)

        def new_note():
            selected["id"] = None
            title_entry.delete(0, "end")
            content_box.delete("1.0", "end")
            meta.configure(text="New note")
            project_choices(self.project_view if isinstance(self.project_view, int) else None)
            refresh_notes()
            title_entry.focus_set()

        def save_note():
            title = title_entry.get().strip()
            text = content_box.get("1.0", "end").strip()
            if not title:
                messagebox.showwarning("Notes", "Give the note a title.", parent=win)
                title_entry.focus_set()
                return
            try:
                selected["id"] = save_note_db(title, text, selected["id"],
                                              project_id=note_projects.get(note_project_var.get()))
                meta.configure(text="Saved just now")
                refresh_notes()
                self.set_status("Note saved")
            except Exception as exc:
                messagebox.showerror("Notes", str(exc), parent=win)

        def delete_note():
            if selected["id"] is None:
                new_note()
                return
            if not messagebox.askyesno("Delete note", "Permanently delete this note?", parent=win):
                return
            try:
                delete_note_db(selected["id"])
                new_note()
                self.set_status("Note deleted")
            except Exception as exc:
                messagebox.showerror("Notes", str(exc), parent=win)

        self._button(left, "+  New note", new_note, kind="secondary").pack(fill="x", pady=(0, 10))
        note_list.pack(fill="both", expand=True)
        self._button(actions, "Save", save_note, width=96).pack(side="right")
        self._button(actions, "Delete", delete_note, kind="danger", width=96).pack(side="right", padx=8)

        # ---- Reminders ----
        self._build_reminders_tab(win, reminders_tab)
        refresh_notes()
        if tab:
            tabs.set(tab)

    # -------------------- tray --------------------
    def start_tray_icon(self):
        if not self.tray_dependencies or self.tray_icon is not None:
            return
        pystray, _Image, _ImageDraw = self.tray_dependencies
        image = nova_icon_image(64)

        def open_window(icon, item):
            self.root.after(0, self.show_window)

        def hide_window(icon, item):
            self.root.after(0, self.hide_to_tray)

        def exit_app(icon, item):
            self.root.after(0, self.exit_application)

        def toggle_hands_free(icon, item):
            self.root.after(0, self.toggle_wake_word)

        def toggle_pause(icon, item):
            self.root.after(0, self.toggle_voice_pause)

        def stop_speaking(icon, item):
            SPEAKER.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open Nova", open_window, default=True),
            pystray.MenuItem("Hide to tray", hide_window),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Hands-free listening", toggle_hands_free, checked=lambda item: self.wake_active),
            pystray.MenuItem(lambda item: "Resume listening" if self.voice_paused.is_set() else "Pause listening",
                             toggle_pause, visible=lambda item: self.wake_active),
            pystray.MenuItem("Stop speaking", stop_speaking),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit Nova", exit_app),
        )
        self.tray_icon = pystray.Icon("NovaAI", image, "Nova", menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True, name="NovaTray")
        self.tray_thread.start()

    def show_window(self):
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_to_tray(self):
        if not self.tray_dependencies or not self.tray_icon:
            self.set_status("System tray unavailable; install pystray and pillow")
            return
        try:
            self.root.withdraw()
            self.set_status("Running in system tray")
        except tk.TclError:
            pass

    # -------------------- Settings --------------------
    def open_startup_settings(self):
        """Settings window: General (startup, tray), Model, and Voice & microphone."""
        existing = getattr(self, "_settings_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

        f = self.fonts
        win = self._dialog("Settings", "780x700", (700, 580))
        self._settings_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(
            body, "Settings",
            "Changes apply as soon as you save. Anything that needs a restart or sign-in says so.",
        )

        # Worker threads hand UI updates back to Tk's thread through this queue.
        ui_queue = queue.Queue()

        def pump():
            try:
                while True:
                    ui_queue.get_nowait()()
            except queue.Empty:
                pass
            except tk.TclError:
                return  # window closed while an update was pending
            except Exception:
                log.exception("Settings window update failed")
            try:
                if win.winfo_exists():
                    win.after(100, pump)
            except tk.TclError:
                pass

        # ---- shared building blocks ----
        def card(parent, pady=(8, 0)):
            frame = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=14)
            frame.pack(fill="x", pady=pady, padx=(0, 8))
            return frame

        def divider(parent):
            ctk.CTkFrame(parent, height=1, fg_color=BORDER).pack(fill="x", padx=18, pady=(14, 0))

        def end_card(parent):
            ctk.CTkFrame(parent, height=16, fg_color="transparent").pack(fill="x")

        def note_label(parent, text="", color=MUTED):
            label = ctk.CTkLabel(parent, text=text, font=f.ui(12), text_color=color, anchor="w",
                                 justify="left", wraplength=560)
            label.pack(fill="x", padx=18, pady=(6, 0))
            return label

        def heading(parent, title, description=None):
            box = ctk.CTkFrame(parent, fg_color="transparent")
            box.pack(fill="x", padx=18, pady=(16, 0))
            ctk.CTkLabel(box, text=title, font=f.ui(14, semibold=True), text_color=TEXT, anchor="w").pack(fill="x")
            if description:
                ctk.CTkLabel(box, text=description, font=f.ui(12), text_color=MUTED, anchor="w",
                             justify="left", wraplength=560).pack(fill="x", pady=(2, 0))
            controls = ctk.CTkFrame(parent, fg_color="transparent")
            controls.pack(fill="x", padx=18, pady=(8, 0))
            return controls

        def switch_row(parent, title, description, variable, enabled=True):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=(16, 0))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(text, text=title, font=f.ui(14, semibold=True), text_color=TEXT, anchor="w").pack(fill="x")
            desc = ctk.CTkLabel(text, text=description, font=f.ui(12), text_color=MUTED, anchor="w",
                                justify="left", wraplength=480)
            desc.pack(fill="x", pady=(2, 0))
            # A disabled switch shows "off" via a throwaway variable, so the
            # saved setting isn't changed just because the feature is unavailable.
            shown = variable if enabled else tk.BooleanVar(value=False)
            switch = ctk.CTkSwitch(
                row, text="", variable=shown, onvalue=True, offvalue=False, width=46,
                progress_color=ACCENT if enabled else SURFACE_2, fg_color=SURFACE_2,
                button_color=TEXT if enabled else SUBTLE,
                button_hover_color="#ffffff", switch_width=40, switch_height=22,
                state="normal" if enabled else "disabled",
            )
            switch.pack(side="right", padx=(12, 0))
            return switch, desc

        def option_menu(parent, variable, width=420):
            menu = ctk.CTkOptionMenu(
                parent, variable=variable, values=["Loading…"], width=width, height=36,
                corner_radius=10, dynamic_resizing=False, font=f.ui(13), dropdown_font=f.ui(13),
                fg_color=SURFACE_2, button_color=SURFACE_2, button_hover_color=BORDER,
                text_color=TEXT, text_color_disabled=SUBTLE,
                dropdown_fg_color=SURFACE, dropdown_hover_color=SURFACE_2, dropdown_text_color=TEXT,
            )
            menu.pack(side="left")
            return menu

        def slider(parent, variable, low, high, steps, fmt):
            value_label = ctk.CTkLabel(parent, text=fmt(variable.get()), font=f.ui(13), text_color=TEXT,
                                       width=80, anchor="e")
            control = ctk.CTkSlider(
                parent, from_=low, to=high, number_of_steps=steps, variable=variable, height=18,
                progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACCENT_HOVER, fg_color=SURFACE_2,
                command=lambda value: value_label.configure(text=fmt(value)),
            )
            control.pack(side="left", fill="x", expand=True)
            value_label.pack(side="left", padx=(10, 0))
            return control

        def unique_label(label, used):
            candidate, n = label, 2
            while candidate in used:
                candidate, n = f"{label} ({n})", n + 1
            return candidate

        # ---- window frame: buttons first so they're never pushed off-screen ----
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(14, 0))
        tabs = ctk.CTkTabview(
            body, fg_color=BG, corner_radius=12, anchor="w",
            segmented_button_fg_color=SURFACE, segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=SURFACE, segmented_button_unselected_hover_color=SURFACE_2,
            text_color=TEXT,
        )
        tabs.pack(fill="both", expand=True)
        general_tab = self._scroll_list(tabs.add("General"))
        general_tab.pack(fill="both", expand=True)
        model_tab = self._scroll_list(tabs.add("Model"))
        model_tab.pack(fill="both", expand=True)
        voice_tab = self._scroll_list(tabs.add("Voice & microphone"))
        voice_tab.pack(fill="both", expand=True)
        online_tab = tabs.add("Online")
        backup_tab = self._scroll_list(tabs.add("Backup & restore"))
        backup_tab.pack(fill="both", expand=True)
        diagnostics_tab = tabs.add("Diagnostics")
        # Built the first time they're opened, so Settings itself opens quickly.
        lazy_tabs = {"Online": lambda: self._build_online_tab(win, online_tab),
                     "Backup & restore": lambda: self._build_backup_tab(win, backup_tab),
                     "Diagnostics": lambda: self._build_diagnostics_tab(win, diagnostics_tab)}

        def build_current_tab():
            builder = lazy_tabs.pop(tabs.get(), None)
            if builder:
                builder()

        tabs.configure(command=build_current_tab)
        win.build_current_tab = build_current_tab
        try:
            tabs._segmented_button.configure(font=f.ui(13, semibold=True), height=34)
        except Exception:
            pass

        # ==================== General ====================
        startup_var = tk.BooleanVar(value=startup_enabled())
        tray_var = tk.BooleanVar(value=bool(self.settings.get("minimize_to_tray", True)))
        tray_available = bool(self.tray_dependencies)

        general = card(general_tab)
        switch_row(general, "Start with Windows",
                   "Open Nova automatically when you sign in. Applies to your Windows account only, "
                   "and takes effect the next time you sign in.",
                   startup_var)
        divider(general)
        tray_description = (
            "Closing the window hides Nova in the system tray, so reminders keep working. "
            "Applies immediately."
            if tray_available else
            "Unavailable: run “pip install pystray pillow” in Nova's virtual environment, "
            "then restart Nova."
        )
        switch_row(general, "Keep running in the tray", tray_description, tray_var, enabled=tray_available)
        end_card(general)

        about = card(general_tab, pady=(14, 0))
        for i, (label, value) in enumerate((
            ("Version", NOVA_VERSION),
            ("Model", active_model()),
            ("Speech", "whisper.cpp · Vosk wake word"),
            ("Data", "nova_memory.db on this PC"),
            ("Settings", SETTINGS_PATH.name),
            ("Log", nova_settings.LOG_PATH.name),
        )):
            line = ctk.CTkFrame(about, fg_color="transparent")
            line.pack(fill="x", padx=18, pady=(12 if i == 0 else 2, 12 if label == "Log" else 2))
            ctk.CTkLabel(line, text=label, font=f.ui(13), text_color=MUTED, width=80, anchor="w").pack(side="left")
            ctk.CTkLabel(line, text=value, font=f.ui(13), text_color=TEXT, anchor="w").pack(side="left")

        # ==================== Model ====================
        model_card = card(model_tab)
        model_controls = heading(
            model_card, "Chat model",
            "Models installed in Ollama on this PC. A new choice applies from your next message.",
        )
        model_var = tk.StringVar(value="Loading…")
        model_menu = option_menu(model_controls, model_var)
        model_refresh = self._button(model_controls, "Refresh", lambda: load_models(), kind="secondary", width=90)
        model_refresh.pack(side="left", padx=(8, 0))
        model_note = note_label(model_card)
        note_label(model_card, "To add a model, run “ollama pull <name>” in a terminal, then click Refresh. "
                               "Larger models answer better but need more memory and are slower.", SUBTLE)
        warm_controls = heading(
            model_card, "Load the model",
            "Ollama needs a few seconds to load the model into memory for the first reply after a pause (about "
            "5 s for gemma3:4b on this PC). Nova can start that early. It uses memory (about 3 GB for gemma3:4b) "
            "until Ollama unloads the model after 5 idle minutes.",
        )
        warmup_labels = {"When I start typing": "typing", "When Nova starts": "startup",
                         "Only when I send a message": "off"}
        warmup_var = tk.StringVar(value=next(label for label, value in warmup_labels.items()
                                             if value == self.settings.get("model_warmup", "typing")))
        warmup_menu = option_menu(warm_controls, warmup_var, width=300)
        warmup_menu.configure(values=list(warmup_labels), state="normal")
        divider(model_card)
        context_controls = heading(
            model_card, "Context size",
            "How much the model can read for each reply: recent messages, a summary of older ones, and the "
            "memories, notes, reminders and document excerpts that match your message. Larger sizes keep more "
            "of a long chat but use more memory, and the first reply after changing it reloads the model. "
            "Nova never uses more than the selected model supports.",
        )
        context_labels = {"Compact · 4K tokens (fastest)": "compact", "Standard · 8K tokens": "standard",
                          "Large · 16K tokens (more memory)": "large"}
        context_var = tk.StringVar(value=next((label for label, value in context_labels.items()
                                               if value == self.settings.get("context_size", "compact")),
                                              next(iter(context_labels))))
        context_menu = option_menu(context_controls, context_var, width=300)
        context_menu.configure(values=list(context_labels), state="normal")
        summarize_var = tk.BooleanVar(value=bool(self.settings.get("summarize_long_chats", True)))
        switch_row(model_card, "Summarize long chats",
                   "When a chat gets longer than what's sent each time, Nova asks the local model for a short "
                   "summary of the older part, in the background. The summary is stored with that chat only "
                   "(never as a long-term memory) and is deleted with it.", summarize_var)
        divider(model_card)
        agent_controls = heading(
            model_card, "Agent (“agent: …”)",
            "Limits for requests Nova plans and carries out itself. Anything that changes files waits for you to "
            "approve the plan, and consequential steps ask again right before they run.",
        )
        agent_choices = {
            "agent_max_steps": ("Steps", {"5 steps": 5, "10 steps": 10, "15 steps": 15, "25 steps": 25}),
            "agent_max_retries": ("Retries", {"No retries": 0, "1 retry": 1, "2 retries": 2, "3 retries": 3}),
            "agent_max_replans": ("New plans", {"No new plans": 0, "1 new plan": 1, "2 new plans": 2}),
            "agent_time_limit": ("Time", {"1 minute": 60, "5 minutes": 300, "10 minutes": 600, "30 minutes": 1800}),
        }
        agent_vars = {}
        for key, (_label, options) in agent_choices.items():
            current = self.settings.get(key, nova_settings.DEFAULTS[key])
            label = next((name for name, value in options.items() if value == current),
                         min(options, key=lambda name: abs(options[name] - current)))
            agent_vars[key] = tk.StringVar(value=label)
            menu = option_menu(agent_controls, agent_vars[key], width=130)
            menu.configure(values=list(options), state="normal")
            menu.pack_configure(padx=(0, 8))
        end_card(model_card)
        model_state = {"labels": {}}  # menu label -> model name

        def selected_model():
            return model_state["labels"].get(model_var.get())

        def show_models(names, error):
            current = selected_model() or active_model()
            model_refresh.configure(state="normal")
            if error:
                model_state["labels"] = {current: current}
                model_menu.configure(values=[current], state="disabled")
                model_var.set(current)
                model_note.configure(text=f"{error} The current model stays selected.", text_color=WARNING)
                return
            if current not in names and f"{current}:latest" in names:
                current = f"{current}:latest"
            labels = {name: name for name in names}
            if current not in names:
                labels = {f"{current} (not installed)": current, **labels}
                model_note.configure(
                    text=(f"“{current}” isn't installed. Choose an installed model, or run "
                          f"“ollama pull {current}”." if names else
                          f"No chat models are installed. Run “ollama pull {DEFAULT_MODEL}” in a terminal, "
                          "then click Refresh."),
                    text_color=WARNING,
                )
            else:
                count = len(names)
                model_note.configure(text=f"{count} chat model{'s' if count != 1 else ''} installed.",
                                     text_color=MUTED)
            model_state["labels"] = labels
            model_menu.configure(values=list(labels), state="normal")
            model_var.set(next(label for label, value in labels.items() if value == current))

        def load_models():
            model_refresh.configure(state="disabled")
            model_menu.configure(state="disabled")
            model_note.configure(text="Looking for installed models…", text_color=MUTED)

            def worker():
                try:
                    names, error = nova_settings.list_ollama_models(timeout=5), None
                except nova_settings.OllamaUnavailable as exc:
                    names, error = [], str(exc)
                except Exception as exc:
                    log.exception("Listing Ollama models failed")
                    names, error = [], f"Couldn't list models: {exc}."
                ui_queue.put(lambda: show_models(names, error))

            threading.Thread(target=worker, daemon=True, name="NovaListModels").start()

        # ==================== Voice & microphone ====================
        speech = card(voice_tab)
        voice_controls = heading(speech, "Voice", "Used when Nova speaks replies to voice requests.")
        voice_var = tk.StringVar(value="Loading…")
        voice_menu = option_menu(voice_controls, voice_var)
        voice_menu.configure(state="disabled")
        voice_note = note_label(speech)
        voice_state = {"labels": None}  # menu label -> voice id (None = Windows default)

        saved_rate = self.settings.get("tts_rate") or DEFAULT_TTS_RATE
        rate_var = tk.DoubleVar(value=saved_rate)
        rate_controls = heading(speech, "Speaking speed", f"Words per minute. Nova's default is {DEFAULT_TTS_RATE}.")
        slider(rate_controls, rate_var, nova_settings.TTS_RATE_MIN, nova_settings.TTS_RATE_MAX,
               (nova_settings.TTS_RATE_MAX - nova_settings.TTS_RATE_MIN) // 5, lambda v: f"{int(round(float(v)))} wpm")

        saved_volume = self.settings.get("tts_volume")
        volume_var = tk.DoubleVar(value=100 * (1.0 if saved_volume is None else saved_volume))
        volume_controls = heading(speech, "Volume", "Relative to your Windows volume.")
        slider(volume_controls, volume_var, 0, 100, 20, lambda v: f"{int(round(float(v)))}%")

        test_row = ctk.CTkFrame(speech, fg_color="transparent")
        test_row.pack(fill="x", padx=18, pady=(14, 0))
        test_note = ctk.CTkLabel(test_row, text="", font=f.ui(12), text_color=MUTED, anchor="w")

        def chosen_voice():
            labels = voice_state["labels"]
            return labels.get(voice_var.get()) if labels else self.settings.get("tts_voice")

        def test_voice():
            voice = chosen_voice() or ""  # "" = Windows default, not the saved voice
            rate = int(round(rate_var.get()))
            volume = round(volume_var.get() / 100, 2)
            test_button.configure(state="disabled", text="Speaking…")
            test_note.configure(text="", text_color=MUTED)

            def worker():
                try:
                    backend.speak_text("Hi, I'm Nova. This is how I'll sound.", voice=voice, rate=rate, volume=volume)
                    message, color = "", MUTED
                except Exception as exc:
                    log.warning("Voice test failed: %s", exc)
                    message, color = f"Speech output failed: {exc}", DANGER

                def done():
                    test_button.configure(state="normal", text="Test voice")
                    test_note.configure(text=message, text_color=color)
                ui_queue.put(done)

            threading.Thread(target=worker, daemon=True, name="NovaVoiceTest").start()

        test_button = self._button(test_row, "Test voice", test_voice, kind="secondary", width=110)
        test_button.pack(side="left")
        test_note.pack(side="left", fill="x", expand=True, padx=(10, 0))
        end_card(speech)

        def show_voices(voices, error):
            if error:
                voice_menu.configure(values=["Unavailable"], state="disabled")
                voice_var.set("Unavailable")
                voice_note.configure(text=f"Text-to-speech isn't available: {error}", text_color=WARNING)
                test_button.configure(state="disabled")
                return
            labels = {"Windows default": None}
            for voice_id, name in voices:
                labels[unique_label(name, labels)] = voice_id
            voice_state["labels"] = labels
            saved = self.settings.get("tts_voice")
            current = next((label for label, value in labels.items() if value == saved), None)
            if current is None:
                current = "Windows default"
                voice_note.configure(text="Your saved voice isn't installed any more, so the Windows default "
                                          "voice is used.", text_color=WARNING)
            else:
                voice_note.configure(text=f"{len(voices)} voice{'s' if len(voices) != 1 else ''} installed. "
                                          "Add more in Windows Settings › Time & language › Speech.",
                                     text_color=MUTED)
            voice_menu.configure(values=list(labels), state="normal")
            voice_var.set(current)

        def load_voices():
            def worker():
                try:
                    voices, error = backend.list_voices(), None
                except Exception as exc:
                    log.warning("Listing TTS voices failed: %s", exc)
                    voices, error = [], str(exc)
                ui_queue.put(lambda: show_voices(voices, error))

            threading.Thread(target=worker, daemon=True, name="NovaListVoices").start()

        # ---- microphone ----
        mic = card(voice_tab, pady=(14, 0))
        mic_controls = heading(mic, "Microphone",
                               "Used for the microphone button and “Hey Nova”. Only devices that support "
                               "Nova's recording format are listed.")
        mic_var = tk.StringVar()
        mic_menu = option_menu(mic_controls, mic_var)
        mic_note = note_label(mic)
        note_label(mic, "Plugged in a new microphone? Restart Nova to see it in this list.", SUBTLE)
        try:
            devices, mic_error = nova_settings.list_input_devices(), None
        except RuntimeError as exc:
            log.error("Listing microphones failed: %s", exc)
            devices, mic_error = [], str(exc)
        default_name = next((d["name"] for d in devices if d["index"] == DEFAULT_MICROPHONE), None)
        default_label = f"Default (device {DEFAULT_MICROPHONE}" + (f": {default_name})" if default_name else ")")
        mic_labels = {default_label: None}
        for device in devices:
            mic_labels[unique_label(device["label"], mic_labels)] = device
        saved_mic = self.settings.get("microphone_name")
        current_mic = default_label
        if saved_mic:
            index = nova_settings.resolve_input_device(saved_mic, self.settings.get("microphone_hostapi"), devices)
            current_mic = next((label for label, d in mic_labels.items() if d and d["index"] == index), None)
            if current_mic is None:
                current_mic = f"{saved_mic} (not connected)"
                mic_labels = {current_mic: "keep", **mic_labels}
                mic_note.configure(text="Your saved microphone isn't connected; the default device is in use "
                                        "until it is. Keep it selected to switch back automatically next time.",
                                   text_color=WARNING)
        if mic_error:
            mic_note.configure(text=f"{mic_error} Only the default device can be used.", text_color=WARNING)
        mic_menu.configure(values=list(mic_labels))
        mic_var.set(current_mic)

        divider(mic)
        wake_reason = wake_word_support()
        wake_var = tk.BooleanVar(value=bool(self.settings.get("wake_word_enabled")))
        switch_row(mic, "Hands-free listening",
                   wake_reason or "Listen for the wake phrase whenever Nova is open (also from the tray), fully "
                                  "offline. Same as the Hey Nova switch below the chat box. Without it, use the "
                                  "microphone button (push-to-talk).",
                   wake_var, enabled=wake_reason is None)
        confirm_var = tk.BooleanVar(value=bool(self.settings.get("voice_confirm_transcript", True)))
        switch_row(mic, "Confirm what I said before sending",
                   "On: Nova shows the transcript and asks “Send this to Nova?”. Off: it's sent straight away, "
                   "like a typed message. Applies to the microphone button and to hands-free requests sent to "
                   "regular chat. Anything that changes files still asks for approval.", confirm_var)
        end_card(mic)

        # ---- hands-free (Step 29) ----
        hands = card(voice_tab, pady=(14, 0))
        phrase_controls = heading(hands, "Wake phrase",
                                  "1–4 words the offline wake-word model knows. Two or more words go off by "
                                  "accident less often.")
        phrase_entry = self._entry(phrase_controls, "hey nova", width=220)
        phrase_entry.insert(0, str(self.settings.get("wake_phrase") or nova_voice.DEFAULT_PHRASE))
        phrase_entry.pack(side="left")
        sensitivity_labels = {"Low (fewest false starts)": "low", "Medium": "medium", "High (hears you more easily)": "high"}
        sensitivity_var = tk.StringVar(value=next(
            (k for k, v in sensitivity_labels.items() if v == self.settings.get("wake_sensitivity")), "Medium"))
        sensitivity_menu = option_menu(phrase_controls, sensitivity_var, width=230)
        sensitivity_menu.configure(values=list(sensitivity_labels), state="normal")
        sensitivity_menu.pack_configure(padx=(10, 0))
        phrase_note = note_label(hands)

        divider(hands)
        route_controls = heading(hands, "Spoken requests go to",
                                 "The agent plans each request and asks before changing anything — aloud and on "
                                 "screen; risky steps need you to say “confirm”. Regular chat asks you to confirm "
                                 "each transcript instead.")
        route_labels = {"The agent (recommended)": "agent", "Regular chat": "chat"}
        route_var = tk.StringVar(value=next(
            (k for k, v in route_labels.items() if v == self.settings.get("voice_route")), "The agent (recommended)"))
        route_menu = option_menu(route_controls, route_var, width=260)
        route_menu.configure(values=list(route_labels), state="normal")

        divider(hands)
        timing_controls = heading(hands, "Listening",
                                  "How long Nova waits for you to start, the pause that ends a request, and the "
                                  "longest request.")
        timing_choices = {
            "voice_listen_timeout": {"Wait 4 s": 4, "Wait 6 s": 6, "Wait 10 s": 10, "Wait 15 s": 15},
            "voice_end_silence": {"Pause 0.8 s": 0.8, "Pause 1.2 s": 1.2, "Pause 2 s": 2.0, "Pause 3 s": 3.0},
            "voice_max_utterance": {"Up to 10 s": 10, "Up to 20 s": 20, "Up to 40 s": 40, "Up to 60 s": 60},
        }
        timing_vars = {}
        for key, options in timing_choices.items():
            current = float(self.settings.get(key, nova_settings.DEFAULTS[key]))
            label = min(options, key=lambda name: abs(options[name] - current))
            timing_vars[key] = tk.StringVar(value=label)
            menu = option_menu(timing_controls, timing_vars[key], width=130)
            menu.configure(values=list(options), state="normal")
            menu.pack_configure(padx=(0, 8))
        barge_var = tk.BooleanVar(value=bool(self.settings.get("voice_barge_in", True)))
        switch_row(hands, "The wake phrase interrupts Nova",
                   "Say the wake phrase while Nova is talking to stop it and speak. Esc, the Stop speaking "
                   "button and the tray menu always work.", barge_var)
        beep_var = tk.BooleanVar(value=bool(self.settings.get("voice_wake_sound", True)))
        switch_row(hands, "Beep when Nova starts listening", "A short tone after the wake phrase.", beep_var)
        note_label(hands, "Privacy: the wake phrase is detected on this PC by the offline Vosk model; that audio "
                          "stays in memory and is discarded. A spoken request is transcribed on this PC by "
                          "whisper.cpp from a temporary file that is deleted straight away — Nova never saves "
                          "recordings. Pause listening any time (the microphone closes) from the indicator next "
                          "to the Hey Nova switch or the tray menu.", SUBTLE)
        end_card(hands)

        # ==================== save ====================
        def apply_settings():
            new = dict(self.settings)
            if tray_available:
                new["minimize_to_tray"] = bool(tray_var.get())
            model = selected_model()
            if model and model != active_model():
                new["ollama_model"] = model
            if voice_state["labels"] is not None:
                new["tts_voice"] = voice_state["labels"].get(voice_var.get())
            new["model_warmup"] = warmup_labels.get(warmup_var.get(), "typing")
            new["context_size"] = context_labels.get(context_var.get(), "compact")
            new["summarize_long_chats"] = bool(summarize_var.get())
            for key, (_label, options) in agent_choices.items():
                new[key] = options.get(agent_vars[key].get(), nova_settings.DEFAULTS[key])
            new["tts_rate"] = int(round(rate_var.get()))
            new["tts_volume"] = round(volume_var.get() / 100, 2)
            choice = mic_labels.get(mic_var.get())
            if choice is None:
                new["microphone_name"] = new["microphone_hostapi"] = None
            elif choice != "keep":
                new["microphone_name"], new["microphone_hostapi"] = choice["name"], choice["hostapi"]
            if wake_reason is None:
                new["wake_word_enabled"] = bool(wake_var.get())
            new["voice_confirm_transcript"] = bool(confirm_var.get())
            phrase = nova_voice.normalize(phrase_entry.get()) or nova_voice.DEFAULT_PHRASE
            model_path = find_vosk_model()
            model = _VOSK_MODELS.get(str(model_path)) if model_path else None   # only if already loaded
            problem = nova_voice.WakePhrase.check(phrase, nova_voice.vosk_word_checker(model) if model else None)
            if problem:
                phrase_note.configure(text=problem, text_color=DANGER)
                messagebox.showwarning("Settings", problem, parent=win)
                return
            new["wake_phrase"] = phrase
            new["wake_sensitivity"] = sensitivity_labels.get(sensitivity_var.get(), "medium")
            new["voice_route"] = route_labels.get(route_var.get(), "agent")
            for key, options in timing_choices.items():
                new[key] = options.get(timing_vars[key].get(), nova_settings.DEFAULTS[key])
            new["voice_barge_in"] = bool(barge_var.get())
            new["voice_wake_sound"] = bool(beep_var.get())
            hands_free_keys = ("wake_phrase", "wake_sensitivity", "voice_listen_timeout", "voice_end_silence",
                               "voice_max_utterance", "voice_barge_in", "voice_wake_sound")
            hands_free_changed = any(new.get(k) != self.settings.get(k) for k in hands_free_keys)

            _clean, problems = nova_settings.validate_settings(new)
            if problems:
                messagebox.showwarning("Settings", "\n".join(problems), parent=win)
                return
            old_device = getattr(backend, "MICROPHONE_DEVICE", DEFAULT_MICROPHONE)
            try:
                self.settings = save_nova_settings(new)
            except OSError as exc:
                log.error("Saving settings failed: %s", exc)
                messagebox.showerror("Settings", f"Couldn't save settings to {SETTINGS_PATH}:\n{exc}", parent=win)
                return

            # Its only possible note (saved microphone not connected) is already
            # shown in this window, so it isn't repeated in the chat.
            apply_runtime_settings(self.settings, devices=devices)
            self._update_model_label()
            messages = []

            # Wake word: start/stop to match, restarting it if the microphone changed.
            wanted = bool(self.settings.get("wake_word_enabled"))
            mic_changed = getattr(backend, "MICROPHONE_DEVICE", DEFAULT_MICROPHONE) != old_device
            if wanted and not self.wake_active:
                if self.busy:
                    messages.append("“Hey Nova” will start when Nova finishes the current task.")
                    self.root.after(1000, self._autostart_wake_word)
                elif not self.wake_listener_running():
                    self._wake_autostart = False
                    self.toggle_wake_word(persist=False)
                else:
                    self._restart_wake_listener()
            elif not wanted and self.wake_active:
                self.toggle_wake_word(persist=False)
            elif wanted and (mic_changed or hands_free_changed):
                self._restart_wake_listener()     # the listener reads its settings when it starts
            self._show_voice_state()

            if self.settings.get("minimize_to_tray") and tray_available and self.tray_icon is None:
                self.start_tray_icon()

            # Start with Windows: only touched when the user changed the switch.
            status = "Settings saved"
            if startup_var.get() != startup_enabled():
                try:
                    path = set_startup_enabled(startup_var.get())
                    log.info("Start with Windows %s (%s)", "enabled" if startup_var.get() else "disabled", path)
                    status = ("Settings saved • Nova will start when you sign in" if startup_var.get()
                              else "Settings saved • Nova won't start with Windows")
                except Exception as exc:
                    log.error("Changing Start with Windows failed: %s", exc)
                    messagebox.showerror(
                        "Settings", f"Other settings were saved, but Start with Windows couldn't be changed:\n{exc}",
                        parent=win,
                    )
                    return
            for message in messages:
                self.append_message("system", message)
            self.set_status(status)
            win.destroy()

        self._button(footer, "Save", apply_settings, width=100).pack(side="right")
        self._button(footer, "Cancel", win.destroy, kind="secondary", width=100).pack(side="right", padx=8)

        pump()
        load_models()
        load_voices()

    # -------------------- projects --------------------
    def _saved_project_view(self):
        value = self.settings.get("project_view", "all") if isinstance(self.settings, dict) else "all"
        if value == "none":
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                if with_db(nova_context.get_project, value) is not None:
                    return value
            except sqlite3.Error:
                pass
        return nova_context.ALL

    def _latest_session_in_view(self):
        """The newest chat of the project being viewed (a new one if it has none)."""
        if self.project_view == nova_context.ALL:
            return None
        rows = fetch_sessions(self.project_view)
        if rows:
            return rows[0][0]
        sid = new_session_id()
        if sid is not None:
            self._assign_view_project(sid)
        return sid

    def _assign_view_project(self, session_id):
        if isinstance(self.project_view, int):
            try:
                with_db(nova_context.set_session_project, session_id, self.project_view)
            except (sqlite3.Error, nova_context.ProjectError) as exc:
                log.warning("Couldn't add the chat to the project: %s", exc)

    def _chat_project_id(self):
        try:
            return with_db(nova_context.session_project, self.session_id)
        except sqlite3.Error:
            return None

    def _chat_project(self, session_id=None):
        sid = self.session_id if session_id is None else session_id
        if sid is None:
            return None
        try:
            return with_db(lambda con: nova_context.get_project(con, nova_context.session_project(con, sid)))
        except sqlite3.Error:
            return None

    def _projects(self):
        try:
            return with_db(nova_context.list_projects)
        except sqlite3.Error as exc:
            log.warning("Couldn't list projects: %s", exc)
            return []

    def _refresh_project_menu(self):
        labels = {"All chats": nova_context.ALL, "No project": None}
        for project in self._projects():
            labels[project.name] = project.id
        labels["New project…"] = "new"
        labels["Manage projects…"] = "manage"
        self._project_menu_labels = labels
        self.project_menu.configure(values=list(labels))
        current = next((label for label, value in labels.items()
                        if value == self.project_view and value not in ("new", "manage")), "All chats")
        self.project_var.set(current)

    def _on_project_menu(self, label):
        value = self._project_menu_labels.get(label, nova_context.ALL)
        if value in ("new", "manage"):
            self._refresh_project_menu()     # the menu keeps showing the current view
            self.new_project_dialog() if value == "new" else self.open_projects()
            return
        self.switch_project(value)

    def switch_project(self, view):
        """Show one project's chats (ALL: every chat, None: chats without a project)."""
        if self.busy:
            self.set_status("Wait for Nova to finish before switching projects")
            self._refresh_project_menu()
            return False
        self.project_view = view
        self._persist_setting("project_view", "all" if view == nova_context.ALL else "none" if view is None
                              else int(view))
        if view != nova_context.ALL:
            sessions = fetch_sessions(view)
            if not any(sid == self.session_id for sid, _title, _when in sessions):
                if sessions:
                    self.session_id = sessions[0][0]
                elif self.session_id is not None and not has_messages(self.session_id):
                    try:     # reuse the empty chat that's open instead of adding another
                        with_db(nova_context.set_session_project, self.session_id, view)
                    except (sqlite3.Error, nova_context.ProjectError) as exc:
                        log.warning("Couldn't move the empty chat: %s", exc)
                else:
                    self.session_id = new_session_id()
                    self._assign_view_project(self.session_id)
        self.session_list.limit = self.session_list.PAGE
        self.refresh_sessions(select_current=True)
        self.load_chat()
        self._refresh_project_menu()
        return True

    def _follow_chat_project(self, session_id):
        """Opening a chat from elsewhere (search): show its project in the sidebar."""
        if self.project_view == nova_context.ALL:
            return
        try:
            project = with_db(nova_context.session_project, session_id)
        except sqlite3.Error:
            return
        if project != self.project_view:
            self.project_view = project
            self._persist_setting("project_view", "none" if project is None else int(project))
            self.refresh_sessions(select_current=True)
            self._refresh_project_menu()

    def move_chat(self, session_id, project_id):
        if self.busy and session_id == self.session_id:
            self.set_status("Wait for Nova to finish before moving this chat")
            return
        try:
            with_db(nova_context.set_session_project, session_id, project_id)
        except (sqlite3.Error, nova_context.ProjectError) as exc:
            messagebox.showerror("Nova", f"Couldn't move the chat: {exc}", parent=self.root)
            return
        if session_id == self.session_id and self.project_view not in (nova_context.ALL, project_id):
            self.switch_project(project_id)      # the sidebar follows the open chat
        else:
            self.refresh_sessions(select_current=True)
        project = with_db(nova_context.get_project, project_id) if project_id is not None else None
        self.set_status(f"Moved to {project.name}" if project else "Moved out of the project")

    def _session_menu(self, menu, session_id):
        """Right-click on a chat: Move to project ›"""
        current = with_db(nova_context.session_project, session_id)
        submenu = tk.Menu(menu, tearoff=False)
        submenu.add_command(label=("✓  " if current is None else "     ") + "No project",
                            command=lambda: self.move_chat(session_id, None))
        projects = self._projects()
        if projects:
            submenu.add_separator()
        for project in projects:
            submenu.add_command(label=("✓  " if current == project.id else "     ") + project.name,
                                command=lambda pid=project.id: self.move_chat(session_id, pid))
        submenu.add_separator()
        submenu.add_command(label="New project…", command=lambda: self.new_project_dialog(move_session=session_id))
        menu.add_cascade(label="Move to project", menu=submenu)
        menu.add_separator()

    def new_project_dialog(self, move_session=None, project=None):
        """Create a project (or edit one when project is given)."""
        f = self.fonts
        editing = project is not None
        win = self._dialog("Edit project" if editing else "New project", "520x430", resizable=False)
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(body, "Edit project" if editing else "New project",
                            "A project keeps related chats together. Nova only looks at chats, notes and documents "
                            "of the same project (plus notes and documents that aren't in any project).")
        name = self._entry(body, "Name, e.g. Thesis", width=460)
        name.pack(fill="x")
        ctk.CTkLabel(body, text="Description (optional): what this project is about. Nova sees it in this "
                                "project's chats.", font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                     wraplength=460).pack(fill="x", pady=(12, 4))
        description = ctk.CTkTextbox(body, height=90, corner_radius=10, fg_color=SURFACE_2, border_color=BORDER,
                                     border_width=1, text_color=TEXT, font=f.ui(13), wrap="word")
        description.pack(fill="x")
        if editing:
            name.insert(0, project.name)
            description.insert("1.0", project.description)
        error = ctk.CTkLabel(body, text="", font=f.ui(12), text_color=DANGER, anchor="w")
        error.pack(fill="x", pady=(6, 0))

        def save(_event=None):
            try:
                if editing:
                    with_db(nova_context.update_project, project.id, name.get(), description.get("1.0", "end"))
                    saved = project
                else:
                    saved = with_db(nova_context.create_project, name.get(), description.get("1.0", "end"))
            except (nova_context.ProjectError, sqlite3.Error) as exc:
                error.configure(text=str(exc))
                return
            win.destroy()
            if editing:
                self._refresh_project_menu()
                self._update_title()
                if self._projects_ui:
                    self._projects_ui()
            elif move_session is not None:
                self.move_chat(move_session, saved.id)
                self._refresh_project_menu()
            else:
                self.switch_project(saved.id)
            if self._projects_ui:
                self._projects_ui()

        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x")
        self._button(footer, "Save" if editing else "Create", save, width=110).pack(side="right")
        self._button(footer, "Cancel", win.destroy, kind="secondary", width=100).pack(side="right", padx=8)
        name.bind("<Return>", save)
        win.after(120, name.focus_set)

    def open_projects(self):
        """Manage projects: create, rename, describe, delete (chats are never deleted)."""
        existing = getattr(self, "_projects_window", None)
        try:
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass
        f = self.fonts
        win = self._dialog("Projects", "680x560", (560, 420))
        self._projects_window = win
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(body, "Projects",
                            "Group chats by project. Inside a project, Nova only uses that project's chats, notes and "
                            "documents, plus notes and documents that aren't in any project. Long-term memories and "
                            "reminders are about you, so they're used everywhere.")
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        listing = self._scroll_list(body)
        listing.pack(fill="both", expand=True)

        def delete(project):
            if not messagebox.askyesno(
                    "Delete project",
                    f"Delete the project “{project.name}”?\n\nIts {project.chats} chat(s), notes and documents are "
                    "kept and moved to “No project”. Nothing else is deleted.", parent=win):
                return
            with_db(nova_context.delete_project, project.id)
            if self.project_view == project.id:
                self.switch_project(nova_context.ALL)
            else:
                self.refresh_sessions(select_current=True)
                self._refresh_project_menu()
                self._update_title()
            refresh()

        def refresh():
            if not win.winfo_exists():
                return
            for child in listing.winfo_children():
                child.destroy()
            projects = self._projects()
            if not projects:
                self._empty_state(listing, Icon.FOLDER, "No projects yet",
                                  "Create one to keep related chats, notes and documents together.")
            for project in projects:
                row = ctk.CTkFrame(listing, fg_color=SURFACE, corner_radius=10)
                row.pack(fill="x", pady=3, padx=(0, 8))
                text = ctk.CTkFrame(row, fg_color="transparent")
                text.pack(side="left", fill="x", expand=True, padx=14, pady=10)
                ctk.CTkLabel(text, text=project.name, font=f.ui(14, semibold=True), text_color=TEXT,
                             anchor="w").pack(fill="x")
                detail = f"{project.chats} chat{'s' if project.chats != 1 else ''}"
                if project.description:
                    detail += " · " + " ".join(project.description.split())[:140]
                ctk.CTkLabel(text, text=detail, font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                             wraplength=380).pack(fill="x")
                self._button(row, "Delete", lambda p=project: delete(p), kind="danger", width=80).pack(
                    side="right", padx=(4, 12))
                self._button(row, "Edit", lambda p=project: self.new_project_dialog(project=p), kind="secondary",
                             width=70).pack(side="right", padx=4)
                self._button(row, "Open", lambda p=project: (self.switch_project(p.id), win.destroy()),
                             kind="secondary", width=70).pack(side="right", padx=4)

        self._projects_ui = refresh
        win.bind("<Destroy>", lambda e: setattr(self, "_projects_ui", None) if e.widget is win else None)
        self._button(footer, "New project", self.new_project_dialog, width=130).pack(side="left")
        self._button(footer, "Close", win.destroy, kind="secondary", width=100).pack(side="right")
        refresh()

    def _project_command(self, name, speak):
        """/project [name] typed in a chat."""
        self.busy = False
        self.set_controls_enabled(True)
        self.set_status("Ready")
        projects = self._projects()
        listed = ", ".join(f"“{p.name}”" for p in projects)
        lowered = name.casefold()
        if not name:
            project = self._chat_project()
            text = (f"This chat is in the project “{project.name}”." if project else "This chat isn't in a project.")
            text += (f" Your projects: {listed}. Type /project followed by a name to switch." if projects else
                     " You have no projects yet. Type /project new followed by a name to create one.")
            self.chat.add_info(text)
            return
        if lowered.startswith("new "):
            try:
                project = with_db(nova_context.create_project, name[4:])
            except (nova_context.ProjectError, sqlite3.Error) as exc:
                self.chat.add_info(str(exc), error=True)
                return
            if self.switch_project(project.id):
                self.chat.add_info(f"Created the project “{project.name}”. New chats in it only use its own chats, "
                                   "notes and documents.")
            return
        if lowered in ("all", "all chats"):
            self.switch_project(nova_context.ALL)
            self.chat.add_info("Showing all chats.")
            return
        if lowered in ("none", "no project"):
            self.switch_project(None)
            self.chat.add_info("Showing chats that aren't in a project.")
            return
        project = with_db(nova_context.find_project, name)
        if project is None:
            self.chat.add_info(f"There's no project called “{name}”." +
                               (f" Your projects: {listed}." if projects else "") +
                               f" To create it, type /project new {name}.", error=True)
            return
        if self.switch_project(project.id):
            self.chat.add_info(f"Switched to the project “{project.name}”.")

    # -------------------- context: links, inspector, summaries --------------------
    def _add_context_link(self, message_id, text):
        self.chat.add_link(text, lambda mid=message_id: self.open_context_inspector(mid))

    def _show_pending_link(self):
        pending, self._pending_link = self._pending_link, None
        if pending and pending[0] == self.session_id and self._chat_latest:
            self._add_context_link(pending[1], pending[2])

    def open_context_inspector(self, message_id=None, session_id=None, from_chat=False):
        """“What Nova used”: everything that was sent with one answer, and what was left out."""
        if from_chat:
            self.busy = False
            self.set_controls_enabled(True)
            self.set_status("Ready")
        try:
            if message_id is not None:
                record = with_db(nova_context.record_for_message, message_id)
            else:
                record = with_db(nova_context.latest_record, session_id or self.session_id)
        except sqlite3.Error as exc:
            self.chat.add_info(f"Couldn't read what Nova used: {exc}", error=True)
            return
        if record is None:
            self.chat.add_info("Nova hasn't recorded what it used for an answer in this chat yet. "
                               "Ask something first, then type /context.")
            return
        f = self.fonts
        d = record.details
        win = self._dialog("What Nova used", "760x700", (600, 460))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        when = record.created_at[:16].replace("T", " ")
        self._dialog_header(body, "What Nova used",
                            f"For the answer of {when}. Only this was sent to the model, after Nova's rules. "
                            "Saved information is sent as data: it can't change Nova's rules or permissions.")
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        listing = self._scroll_list(body)
        listing.pack(fill="both", expand=True)

        def heading(text):
            ctk.CTkLabel(listing, text=text, font=f.ui(13, semibold=True), text_color=MUTED, anchor="w").pack(
                fill="x", padx=4, pady=(12, 3))

        def card(title, detail, excerpt=None, tone=TEXT):
            row = ctk.CTkFrame(listing, fg_color=SURFACE, corner_radius=10)
            row.pack(fill="x", pady=2, padx=(0, 8))
            ctk.CTkLabel(row, text=title, font=f.ui(13, semibold=True), text_color=tone, anchor="w", justify="left",
                         wraplength=640).pack(fill="x", padx=14, pady=(9, 0))
            if detail:
                ctk.CTkLabel(row, text=detail, font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                             wraplength=640).pack(fill="x", padx=14, pady=(1, 0))
            if excerpt:
                ctk.CTkLabel(row, text=excerpt, font=f.ui(12), text_color=SUBTLE, anchor="w", justify="left",
                             wraplength=640).pack(fill="x", padx=14, pady=(3, 0))
            ctk.CTkFrame(row, height=8, fg_color="transparent").pack()

        heading("Your message")
        card(" ".join(str(d.get("request", "")).split())[:400], None)
        if d.get("local_answer"):
            heading("Answered without the model")
            card("Nova asked instead of guessing",
                 nova_context.LOCAL_REASONS.get(d["local_answer"], "") + ".", tone=WARNING)
        history = d.get("history") or {}
        heading("Conversation")
        project = d.get("project")
        card(f"The last {history.get('sent', 0)} of {history.get('total', 0)} messages in this chat",
             (f"Project: {project['name']}" if project else "Not in a project") +
             ("" if not history.get("shortened") else " · " + " ".join(history["shortened"][:2])))
        groups = {}
        for item in d.get("items", []):
            if item["kind"] != "project":
                groups.setdefault(item["kind"], []).append(item)
        for kind, (one, many) in nova_context.KIND_LABELS.items():
            items = groups.get(kind)
            if not items:
                continue
            heading((many if len(items) > 1 else one).capitalize())
            for item in items:
                excerpt = " ".join(str(item.get("text", "")).split())
                if kind == "memory":
                    excerpt = ""          # the title already is the memory
                card(item["label"], item["reason"], excerpt[:300] + ("…" if len(excerpt) > 300 else ""))
        if not groups:
            heading("Saved information")
            card("None", "Nothing saved matched this message, so only the conversation was sent.")
        if d.get("skipped"):
            heading("Relevant but left out")
            for skipped in d["skipped"]:
                card(skipped["label"], skipped["why"], tone=MUTED)
        if d.get("not_found"):
            heading("Searched, nothing found")
            for line in d["not_found"]:
                card(line, None, tone=MUTED)
        if d.get("removed"):
            heading("Removed before sending")
            for entry in d["removed"]:
                count = entry["sentences"]
                card(entry["label"], f"{count} sentence{'s' if count != 1 else ''} that read like an instruction to an "
                     "AI (small local models tend to follow those). Your saved text isn't changed.", tone=WARNING)
        budget = d.get("budget") or {}
        if budget:
            heading("Context size")
            used = int(budget.get("prompt_chars", 0) / nova_context.CHARS_PER_TOKEN)
            card(f"About {used:,} of {budget.get('num_ctx', 0):,} tokens",
                 "Change it in Settings › Model › Context size." +
                 (f" This model supports up to {budget['model_max']:,}." if budget.get("model_max") else ""))

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(nova_context.record_text(record))
            self.set_status("Copied what Nova used")

        def forget_summary():
            if messagebox.askyesno("Forget summary", "Delete Nova's summary of this chat's older messages? "
                                   "The messages themselves are kept; Nova can write a new summary later.",
                                   parent=win):
                with_db(nova_context.clear_summary, record.session_id)
                self.set_status("Chat summary deleted")
                win.destroy()

        if "summary" in groups:
            self._button(footer, "Forget chat summary", forget_summary, kind="danger", width=170).pack(side="left")
        self._button(footer, "Close", win.destroy, kind="secondary", width=100).pack(side="right")
        self._button(footer, "Copy as text", copy, kind="secondary", width=130).pack(side="right", padx=8)

    def _maybe_summarize(self):
        """After a reply, summarize messages that have left the history window (in the background)."""
        if self._summarizing or self.busy or self._exiting or not self.settings.get("summarize_long_chats", True):
            return
        session_id, model, settings = self.session_id, active_model(), dict(self.settings)
        if session_id is None:
            return
        try:
            budget = context_budget(model, settings) if model in _MODEL_CONTEXT else nova_context.budget_for(
                settings.get("context_size") or nova_context.DEFAULT_CONTEXT_SIZE)
            job = with_db(nova_context.summary_job, session_id, budget)
        except sqlite3.Error as exc:
            log.warning("Chat summary check failed: %s", exc)
            return
        if job is None:
            return
        self._summarizing = True
        self._summary_cancel.clear()
        cancel = self._summary_cancel

        def worker():
            started = time.perf_counter()
            try:
                stream = ollama.chat(model=model, messages=nova_context.summary_messages(job), stream=True,
                                     options={"num_ctx": budget.num_ctx, "num_predict": 320, "temperature": 0.2})
                parts, done = [], False
                chunks = cancellable_stream(stream, cancel)
                try:
                    for chunk in chunks:
                        if cancel.is_set():
                            break
                        parts.append(_stream_piece(chunk) or "")
                        done = done or _chunk_done(chunk)
                finally:
                    chunks.close()
                if done and not cancel.is_set() and with_db(nova_context.save_summary, job, "".join(parts), model):
                    nova_perf.record("chat summary (background)", (time.perf_counter() - started) * 1000)
                    log.info("Summarized %d older messages of a chat", len(job.rows))
            except Exception as exc:
                log.info("Chat summary skipped: %s", exc)
            finally:
                self._summarizing = False

        threading.Thread(target=worker, daemon=True, name="NovaChatSummary").start()

    # -------------------- optional online integrations (Step 26) --------------------
    def _online_http(self):
        if self._http is None:
            self._http = nova_online.Http()
        return self._http

    def _online_reply(self, session_id, text, message, speak):
        """Worker thread: a short online-related reply (refusal, error, “nothing was sent”)."""
        try:
            save_message_safe(session_id, "user", text)
            save_message_safe(session_id, "assistant", message)
        except Exception as exc:
            message += f"\n\n(Note: couldn't save this exchange to chat history: {exc})"
        self.events.put(("action_done", (message, speak)))

    def _online_request(self, session_id, text, kind, query, speak):
        """Worker thread: an explicit online request (web:, wiki:, weather in …).

        Order: switched on? → provider available? → show exactly what will be sent and
        ask → fetch (errors become messages) → answer with the local model, citing sources.
        """
        try:
            provider = nova_online.choose(kind, self.settings, self.secrets)
        except nova_online.OnlineError as exc:
            self._online_reply(session_id, text, str(exc), speak)
            return
        problem = nova_online.check_query(kind, query)
        if problem:
            self._online_reply(session_id, text, problem, speak)
            return
        disclosure = provider.disclose(query)
        if nova_online.needs_consent(disclosure, self.settings):
            answer = queue.Queue()
            self.events.put(("online_consent", (disclosure, answer)))
            try:
                approved = answer.get(timeout=300)
            except queue.Empty:
                approved = False
            if not approved:
                self._online_reply(session_id, text, f"Okay, nothing was sent to {provider.name}.", speak)
                return
        self.events.put(("status", f"Asking {provider.name}…"))
        started = time.perf_counter()
        try:
            retrieval = nova_online.retrieve(provider, query, self._online_http(), self.secrets)
        except nova_online.OnlineError as exc:
            self._online_reply(session_id, text, "🌐 " + str(exc), speak)
            return
        nova_perf.record(f"online: {provider.name}", (time.perf_counter() - started) * 1000)
        if not retrieval.results:
            self._online_reply(session_id, text, nova_online.no_results(retrieval), speak)
            return
        self._answer_with_web(session_id, text, retrieval, speak)

    def _answer_with_web(self, session_id, text, retrieval, speak):
        """Worker thread: the local model answers from the retrieved results; Nova labels the
        answer as online and adds the real source list itself."""
        model = active_model()
        cancel = _cancel_event(self)
        self.events.put(("assistant_start", None))
        self.events.put(("doc_reply_started", None))        # progress and the Stop button
        prefix = nova_online.label_line(retrieval)
        parts, first, final, started = [], None, None, time.perf_counter()
        try:
            user_id = save_message_safe(session_id, "user", text)
            budget = context_budget(model, self.settings)
            ctx = build_context(session_id, text, budget)
            ctx.local_answer = ctx.local_reason = None       # the user asked to look this up online
            nova_online.add_to_context(ctx, retrieval)
            messages = ctx.messages()
            self.events.put(("token", prefix))
            stream = ollama.chat(model=model, messages=messages, stream=True,
                                 options={"num_ctx": budget.num_ctx, "temperature": 0.2})
            chunks = cancellable_stream(stream, cancel)
            try:
                for chunk in chunks:
                    if cancel.is_set():
                        break
                    piece = _stream_piece(chunk)
                    if piece:
                        if first is None:
                            first = time.perf_counter() - started
                            self.events.put(("reply_started", None))
                        parts.append(piece)
                        self.events.put(("token", piece))
                    if _chunk_done(chunk):
                        final = chunk
            finally:
                chunks.close()
            record_reply_timings("online answer", started, first, final)
            answer = "".join(parts).strip()
            if cancel.is_set() and final is None:
                answer = (answer + "\n\n" if answer else "") + "_(Stopped.)_"
                self.events.put(("token", "\n\n_(Stopped.)_"))
            footer = nova_online.sources_block(retrieval, answer)
            self.events.put(("token", "\n\n" + footer))
            full = prefix + (answer or "(No answer was written.)") + "\n\n" + footer
            message_id = save_message_safe(session_id, "assistant", full)
            record_context(self, ctx, message_id)
            try:
                with_db(nova_context.save_sent, {user_id: messages[-1]["content"], message_id: "".join(parts)})
            except Exception as exc:
                log.warning("Couldn't store the sent messages: %s", exc)
            spoken = answer
        except sqlite3.Error as exc:
            log.error("Online answer couldn't be saved: %s", exc)
            self.events.put(("generation_error", f"Nova couldn't save to its database: {exc}"))
            return
        except Exception as exc:
            # The local model isn't available: show the results themselves, clearly labelled.
            log.warning("Online answer without the model: %s", type(exc).__name__)
            fallback = nova_online.results_only(retrieval, "Nova's local model couldn't be reached, so it didn't "
                                                           "write an answer. ")
            self.events.put(("token", "\n\n" + fallback if parts else fallback))
            try:
                save_message_safe(session_id, "assistant", fallback)
            except Exception:
                pass
            spoken = ""
        if speak and spoken:
            try:
                speak_text(spoken)
            except Exception as exc:
                log.warning("Speech output failed: %s", exc)
        self.events.put(("generation_done", None))

    def _confirm_online(self, disclosure, answer):
        """Main thread: show exactly what will be sent, and to whom."""
        sensitive = bool(disclosure.sensitive)
        answer.put(messagebox.askyesno(
            f"Send this to {disclosure.provider}?", disclosure.text(),
            icon="warning" if sensitive else "question", default="no" if sensitive else "yes", parent=self.root))

    def _build_online_tab(self, win, tab):
        """Settings › Online: the master switch, asking before requests, providers and keys."""
        f = self.fonts
        page = self._scroll_list(tab)
        page.pack(fill="both", expand=True)

        def card():
            box = ctk.CTkFrame(page, fg_color=SURFACE, corner_radius=12)
            box.pack(fill="x", pady=(8, 4), padx=(0, 8))
            return box

        def switch(parent, title, description, value, command):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=(12, 4))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(text, text=title, font=f.ui(14, semibold=True), text_color=TEXT, anchor="w").pack(fill="x")
            ctk.CTkLabel(text, text=description, font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=520).pack(fill="x")
            var = tk.BooleanVar(value=value)
            ctk.CTkSwitch(row, text="", variable=var, onvalue=True, offvalue=False, progress_color=ACCENT,
                          fg_color=SURFACE_2, button_color=TEXT, switch_width=40, switch_height=20,
                          command=lambda: command(bool(var.get()))).pack(side="right")
            return var

        main = card()
        switch(main, "Use online integrations",
               "Off by default. When on, Nova goes online only when you ask with “web: …”, “wiki: …” or “weather "
               "in …”. Chat, voice, memories, notes, reminders, documents and tasks never need the internet.",
               nova_online.enabled(self.settings), lambda on: (self._persist_setting("online_enabled", on), render()))
        switch(main, "Ask before each request",
               "Shows exactly what will be sent and to which service. Text that looks personal (emails, phone "
               "numbers, passwords, file paths) always asks.",
               bool(self.settings.get("online_confirm", True)), lambda on: self._persist_setting("online_confirm", on))
        ctk.CTkFrame(main, height=8, fg_color="transparent").pack()
        providers_box = ctk.CTkFrame(page, fg_color="transparent")
        providers_box.pack(fill="x")
        ctk.CTkLabel(page, text="What is sent: only the text after the command (for weather, the place name and then "
                                "its coordinates). Never chats, memories, notes, documents, files or settings. "
                                "Results are shown with their sources, treated as untrusted text, and never trigger "
                                "actions on this PC. API keys are kept in Windows Credential Manager, not in Nova's "
                                "files or backups.", font=f.ui(12), text_color=SUBTLE, anchor="w", justify="left",
                     wraplength=600).pack(fill="x", padx=4, pady=(10, 4))

        def set_provider(key, on):
            flags = dict(self.settings.get("online_providers") or {})
            flags[key] = on
            self._persist_setting("online_providers", flags)

        def render():
            for child in providers_box.winfo_children():
                child.destroy()
            for provider in nova_online.PROVIDERS.values():
                box = ctk.CTkFrame(providers_box, fg_color=SURFACE, corner_radius=12)
                box.pack(fill="x", pady=4, padx=(0, 8))
                switch(box, provider.name, f"{provider.description} Sends to: {', '.join(provider.hosts)}. "
                                           f"Terms: {provider.terms}",
                       nova_online.provider_enabled(self.settings, provider.key),
                       lambda on, key=provider.key: set_provider(key, on))
                if getattr(provider, "needs_contact", False):
                    row = ctk.CTkFrame(box, fg_color="transparent")
                    row.pack(fill="x", padx=16, pady=(2, 12))
                    contact = self._entry(row, "Contact for its API rules: your email or a web address", width=380,
                                          font_size=12, height=32)
                    contact.insert(0, self.settings.get("online_contact") or "")
                    contact.pack(side="left")
                    status = ctk.CTkLabel(row, text="", font=f.ui(12), text_color=MUTED, anchor="w")

                    def save_contact(_event=None, entry=contact, label=status):
                        value = entry.get().strip()
                        if value and not nova_online.valid_contact(value):
                            label.configure(text="Use an email address or an https:// web address.",
                                            text_color=DANGER)
                            return
                        self._persist_setting("online_contact", value)
                        label.configure(text="Saved." if value else "Cleared.", text_color=SUCCESS)

                    self._button(row, "Save", save_contact, kind="secondary", width=70).pack(side="left", padx=8)
                    status.pack(side="left", fill="x", expand=True)
                    contact.bind("<Return>", save_contact)
                if provider.needs_key:
                    row = ctk.CTkFrame(box, fg_color="transparent")
                    row.pack(fill="x", padx=16, pady=(2, 12))
                    try:
                        has_key = self.secrets.has(provider.key)
                    except OSError:
                        has_key = False
                    ctk.CTkLabel(row, text=("API key saved in Windows Credential Manager." if has_key else
                                            provider.key_help), font=f.ui(12),
                                 text_color=SUCCESS if has_key else MUTED, anchor="w", justify="left",
                                 wraplength=440).pack(side="left", fill="x", expand=True)
                    if has_key:
                        self._button(row, "Remove key", lambda p=provider: remove_key(p), kind="secondary",
                                     width=110).pack(side="right")
                    else:
                        self._button(row, "Add key…", lambda p=provider: add_key(p), kind="secondary",
                                     width=100).pack(side="right")
                else:
                    ctk.CTkFrame(box, height=6, fg_color="transparent").pack()

        def add_key(provider):
            dialog = self._dialog(f"{provider.name} API key", "460x260", resizable=False)
            body = ctk.CTkFrame(dialog, fg_color=BG)
            body.pack(fill="both", expand=True, padx=22, pady=18)
            ctk.CTkLabel(body, text=f"Paste your {provider.name} API key. It's stored in Windows Credential Manager "
                                    "and never shown again or written to Nova's files or logs.", font=f.ui(12),
                         text_color=MUTED, anchor="w", justify="left", wraplength=410).pack(fill="x")
            entry = self._entry(body, "API key", width=410, show="•")
            entry.pack(fill="x", pady=12)
            error = ctk.CTkLabel(body, text="", font=f.ui(12), text_color=DANGER, anchor="w")
            error.pack(fill="x")

            def save(_event=None):
                try:
                    self.secrets.set(provider.key, entry.get())
                except (ValueError, OSError) as exc:
                    error.configure(text=str(exc))
                    return
                entry.delete(0, "end")
                dialog.destroy()
                render()

            footer = ctk.CTkFrame(body, fg_color="transparent")
            footer.pack(side="bottom", fill="x")
            self._button(footer, "Save", save, width=90).pack(side="right")
            self._button(footer, "Cancel", dialog.destroy, kind="secondary", width=90).pack(side="right", padx=8)
            entry.bind("<Return>", save)
            dialog.after(120, entry.focus_set)

        def remove_key(provider):
            if messagebox.askyesno("Remove key", f"Remove the {provider.name} API key from Windows Credential "
                                                 "Manager?", parent=win):
                try:
                    self.secrets.delete(provider.key)
                except OSError as exc:
                    messagebox.showerror("Remove key", str(exc), parent=win)
                render()

        render()

    # -------------------- agent engine (Step 28) --------------------
    AGENT_USAGE = ("Describe what Nova should do after “agent:”, for example “agent: find my latest invoice in "
                   "Downloads, copy it to my Tax folder and remind me tomorrow at 9 to file it”. Nova plans the "
                   "steps, shows them before changing anything, and reports what it verifiably did.")

    def _run_agent(self, session_id, text, request, speak, voice=False):
        """Worker thread: plan, approve and run an agent request; the window shows progress.

        voice: asked hands-free, so approvals can also be answered aloud and important
        moments are announced. The same approvals are required either way.
        """
        if not request:
            self._online_reply(session_id, text, self.AGENT_USAGE, speak)
            return
        self._agent_voice = voice
        model = active_model()
        limits = nova_agent.limits_from(self.settings)
        budget = context_budget(model, self.settings)
        planner = nova_agent.Planner(ollama.chat, model, model_capabilities(model), num_ctx=budget.num_ctx,
                                     timeout=limits["model_timeout"])
        env = nova_agent.Env(
            services=self.task_runner.services, settings=dict(self.settings), connect=get_db_connection,
            approved=set(), project_id=self._chat_project_id(),
            online=(self._online_http(), self.secrets) if nova_online.enabled(self.settings) else None)
        self._agent_cancel = threading.Event()

        def on_event(kind, data):
            self.events.put(("agent_event", (kind, data)))
            if voice:
                said = nova_voice.announce(kind, data)
                if said:
                    self._speak_quietly(said)

        engine = nova_agent.AgentEngine(
            self.agent_registry, planner, env, get_db_connection, limits,
            on_event=on_event,
            approve_plan=lambda plan: self._agent_ask("plan", plan),
            approve_step=lambda step, prepared: self._agent_ask("step", (step, prepared)),
            approve_folders=lambda step, folders: self._agent_ask("folders", (step, folders)),
            pick=lambda step, name, kind: self._agent_ask("pick", (step, kind)),
            lock=self.task_runner._lock)
        self.events.put(("agent_event", ("open", request)))
        log.info("Agent task started (%s, %s)", model, "native tools" if planner.native else "structured JSON")
        try:
            report = engine.run(request, self._agent_cancel, session_id)
            reply = report.text()
            spoken = nova_voice.spoken_report(report)     # the outcome, not every step
        except Exception as exc:             # never leave the window busy
            log.exception("Agent task crashed")
            reply = f"The agent stopped because of an unexpected error: {exc}. Nothing more was done."
            spoken = "The task stopped because of an unexpected error. Nothing more was done."
        self._voice_approvals.clear()
        self._online_reply(session_id, text, reply, spoken if speak else False)
        ui = getattr(self, "_tasks_ui", None)
        if ui:
            self.events.put(("status", "Ready"))

    def _agent_ask(self, kind, payload, timeout=600):
        """Worker thread → main thread: ask the user, wait for the answer (no answer means no)."""
        answer = queue.Queue()
        self.events.put(("agent_ask", (kind, payload, answer)))
        try:
            return answer.get(timeout=timeout)
        except queue.Empty:
            return None if kind == "pick" else False

    def _agent_answer(self, kind, payload, answer):
        """Main thread: plan approval in the agent window; step, folder and file questions as dialogs.

        For a spoken request (with hands-free listening on), plan, step and folder
        questions are asked aloud and shown in the agent window; a spoken answer or a
        click answers them, whichever comes first. Consequential steps need “confirm”.
        """
        ui = self._agent_ui
        parent = ui["win"] if ui and ui["win"].winfo_exists() else self.root
        loop = self.voice_loop
        if (self._agent_voice and loop is not None and ui is not None and ui["win"].winfo_exists()
                and kind in ("plan", "step", "folders")):
            self._voice_ask(kind, payload, answer, loop, ui)
            return
        if kind == "pick" and self._agent_voice:
            self.show_window()               # a file can only be chosen on screen
        if kind == "plan":
            if ui is None:
                answer.put(False)
                return
            ui["ask_plan"](answer)
        elif kind == "step":
            step, prepared = payload
            reason = prepared.confirm or nova_agent.SAFETY_LABELS[prepared.safety]
            body = (f"Run this step?\n\n{prepared.description}"
                    + (f"\n\n“{prepared.preview}”" if prepared.preview else "")
                    + f"\n\nNova is asking because it {reason}.\n\nNothing has been changed by this step yet.")
            answer.put(messagebox.askyesno("Confirm step", body, icon="warning", default="no", parent=parent))
        elif kind == "folders":
            _step, folders = payload
            answer.put(messagebox.askyesno(
                "Allow this folder?", "The agent needs to use files in:\n\n" + "\n".join(folders) +
                "\n\nIt isn't one of your allowed folders. Allow it for this task only?", icon="warning",
                default="no", parent=parent))
        elif kind == "pick":
            step, kind_ = payload
            title = f"Choose the {kind_} — {step.purpose or step.tool}"
            chosen = (filedialog.askdirectory(parent=parent, mustexist=True, title=title) if kind_ == "folder"
                      else filedialog.askopenfilename(parent=parent, title=title))
            answer.put(chosen or None)

    def _voice_ask(self, kind, payload, answer, loop, ui):
        """Main thread: ask an agent question aloud and in the agent window; the first answer counts."""
        prompt = nova_voice.approval_prompt(kind, payload)
        approval = loop.ask(prompt, strong=kind != "plan")

        class FirstAnswer:
            def put(_self, value):
                if loop.resolve(approval, value):
                    SPEAKER.stop()           # a click ends the spoken question
                    self._voice_approvals.pop(approval, None)
                    answer.put(value)

        if kind == "plan":
            message = "Review the plan. Say yes or click Run; say no or click Cancel."
            decide = ui["ask"](FirstAnswer(), message, "Run", "Cancel")
        elif kind == "step":
            _step, prepared = payload
            reason = prepared.confirm or nova_agent.SAFETY_LABELS[prepared.safety]
            message = (f"Run this step? {prepared.description}\nNova is asking because it {reason}. "
                       "Say “confirm” or click Approve; say no or click Skip.")
            decide = ui["ask"](FirstAnswer(), message, "Approve", "Skip")
        else:
            _step, folders = payload
            message = ("Allow this folder for this task only? " + ", ".join(folders) +
                       "\nSay “confirm” or click Allow; say no or click Don't allow.")
            decide = ui["ask"](FirstAnswer(), message, "Allow", "Don't allow")
        self._voice_approvals[approval] = decide
        self._speak_quietly(prompt)

    def _agent_event(self, kind, data):
        """Main thread: keep the agent window up to date."""
        if kind == "open":
            self._open_agent_dialog(data)
            return
        ui = self._agent_ui
        if ui is None:
            return
        try:
            ui["event"](kind, data)
        except tk.TclError:
            pass

    def _open_agent_dialog(self, request):
        f = self.fonts
        old = self._agent_ui
        if old and old["win"].winfo_exists():
            old["win"].destroy()
        win = self._dialog("Agent", "720x620", (580, 460))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=(22, 18))
        self._dialog_header(body, "Nova agent", "Nova plans the steps, shows them before changing anything, asks "
                                                "again before anything consequential, and checks every result.")
        ctk.CTkLabel(body, text=f"“{request}”", font=f.ui(13), text_color=MUTED, anchor="w", justify="left",
                     wraplength=640).pack(fill="x", pady=(0, 8))
        status = ctk.CTkLabel(body, text="Planning…", font=f.ui(13, semibold=True), text_color=WARNING, anchor="w",
                              justify="left", wraplength=640)
        status.pack(fill="x", pady=(0, 8))
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        rows_frame = self._scroll_list(body)
        rows_frame.pack(fill="both", expand=True)
        rows = {}
        colors = {"pending": SUBTLE, "running": WARNING, "succeeded": SUCCESS, "failed": DANGER, "timed_out": DANGER,
                  "skipped": SUBTLE, "cancelled": SUBTLE, "declined": WARNING}
        chips = {nova_agent.READ_ONLY: ("reads", SURFACE_2, MUTED), nova_agent.REVERSIBLE: ("changes", "#3a3220",
                                                                                            WARNING),
                 nova_agent.CONSEQUENTIAL: ("asks first", DANGER_SOFT, DANGER)}

        def add_row(step, new=False):
            card = ctk.CTkFrame(rows_frame, fg_color=SURFACE, corner_radius=12)
            card.pack(fill="x", pady=3, padx=(0, 8))
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=14, pady=(10, 4))
            icon = ctk.CTkLabel(top, text=nova_tasks.STEP_ICONS["pending"], width=22, font=f.ui(15, semibold=True),
                                text_color=SUBTLE)
            icon.pack(side="left")
            title = ctk.CTkLabel(top, text=f"{step.id}. {step.description or step.tool}" + ("  (new)" if new else ""),
                                 font=f.ui(14), text_color=TEXT, anchor="w", justify="left", wraplength=470)
            title.pack(side="left", fill="x", expand=True, padx=(4, 0))
            label, fg, color = chips.get(step.safety, chips[nova_agent.READ_ONLY])
            self._chip(top, label, fg, color).pack(side="right")
            detail = ctk.CTkLabel(card, text="", font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                                  wraplength=600)
            rows[step.id] = (icon, title, detail, step)

        def update_row(step):
            if step.id not in rows:
                add_row(step)
            icon, title, detail, _old = rows[step.id]
            icon.configure(text=nova_tasks.STEP_ICONS.get(step.status, "·"), text_color=colors.get(step.status, SUBTLE))
            title.configure(text=f"{step.id}. {step.description or step.tool}")
            text = step.error or (step.output.message.splitlines()[0] if step.output else "")
            if step.evidence and step.status == "succeeded":
                text += f"\nChecked: {step.evidence}"
            if step.attempts > 1:
                text += f"\nTried {step.attempts} times."
            if text:
                detail.configure(text=text, text_color=DANGER if step.status in ("failed", "timed_out") else MUTED)
                if not detail.winfo_ismapped():
                    detail.pack(fill="x", padx=(40, 14), pady=(0, 10))

        def stop():
            if self._agent_cancel is not None:
                self._agent_cancel.set()
            status.configure(text="Stopping after the current step…", text_color=WARNING)
            stop_button.configure(state="disabled")

        def ask(answer, message, yes="Run", no="Cancel"):
            """Show a question with two buttons; returns decide(value), which also answers it (once)."""
            status.configure(text=message, text_color=WARNING)
            stop_button.pack_forget()
            decided = []

            def decide(value):
                if decided:
                    return
                decided.append(value)
                try:
                    run_button.pack_forget()
                    cancel_button.pack_forget()
                    stop_button.configure(state="normal")
                    stop_button.pack(side="right")
                    status.configure(text="Approved." if value else "Declined.", text_color=MUTED)
                except tk.TclError:
                    pass
                answer.put(value)

            run_button.configure(text=yes, command=lambda: decide(True))
            cancel_button.configure(text=no, command=lambda: decide(False))
            run_button.pack(side="right")
            cancel_button.pack(side="right", padx=8)
            win.after(100, run_button.focus_set)
            return decide

        def ask_plan(answer):
            return ask(answer, "Review the plan. Nothing runs until you click Run.")

        def event(kind, data):
            if kind == "planning":
                status.configure(text="Planning…", text_color=WARNING)
            elif kind == "plan":
                if data.notes:                  # e.g. steps left out, a path read differently
                    ctk.CTkLabel(rows_frame, text="\n".join(f"• {n}" for n in data.notes[:6]), font=f.ui(12),
                                 text_color=WARNING, anchor="w", justify="left", wraplength=620).pack(
                        fill="x", padx=4, pady=(0, 6))
                for step in data.steps:
                    add_row(step)
                status.configure(text=f"{len(data.steps)} step(s) planned"
                                      + ("" if data.source != "rules" else " by Nova's own planner") + ".",
                                 text_color=MUTED)
            elif kind == "step":
                update_row(data)
                if data.status == "running":
                    status.configure(text=f"Running: {data.description or data.tool}", text_color=WARNING)
            elif kind == "replanning":
                status.configure(text=f"{data.id} failed. Looking for another safe way…", text_color=WARNING)
            elif kind == "replanned":
                for step in data:
                    add_row(step, new=True)
                status.configure(text=f"{len(data)} new step(s) added.", text_color=MUTED)
            elif kind == "answering":
                status.configure(text="Writing the summary…", text_color=WARNING)
            elif kind == "done":
                text_, color = {"succeeded": ("Done: every step was carried out and checked.", SUCCESS),
                                "cancelled": ("Stopped.", WARNING),
                                "rejected": ("Nothing was done: " + " ".join(data.problems[:2]), DANGER),
                                "timed_out": ("Stopped at the time or step limit.", DANGER)}.get(
                    data.status, ("Not everything worked. See the steps above.", DANGER))
                status.configure(text=text_, text_color=color)
                stop_button.configure(text="Close", state="normal", command=win.destroy)

        run_button = self._button(footer, "Run", lambda: None, width=100)
        cancel_button = self._button(footer, "Cancel", lambda: None, kind="secondary", width=100)
        stop_button = self._button(footer, "Stop", stop, kind="secondary", width=100)
        stop_button.pack(side="right")

        def on_close():
            if self._agent_cancel is not None and not self._agent_cancel.is_set() and self.busy:
                self._agent_cancel.set()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._agent_ui = {"win": win, "event": event, "ask_plan": ask_plan, "ask": ask}

    # -------------------- replies: stop, progress, warm-up --------------------
    def _set_stop_mode(self, on):
        """While a reply is being written, the send button becomes Stop (also Esc)."""
        self._stop_mode = on
        if on:
            self.send_button.configure(state="normal", text=Icon.STOP, command=self.stop_reply,
                                       fg_color=DANGER_SOFT, hover_color="#4a272c", text_color=DANGER)
            self._send_tooltip.text = "Stop the reply (Esc)"
        else:
            self.send_button.configure(text=Icon.SEND, command=self.send_text, hover_color=ACCENT_HOVER,
                                       text_color="#ffffff")
            self._send_tooltip.text = "Send (Enter)"

    def stop_reply(self, _event=None):
        if self.busy and self._stop_mode and not self._cancel_reply.is_set():
            self._cancel_reply.set()
            self.set_status("Stopping…")
            log.info("Reply stopped by the user")
        return "break"

    def _start_reply_progress(self):
        self._cancel_reply.clear()
        self._reply_started = False
        self._reply_began = time.monotonic()
        self._slow_hint_shown = False
        self._model_used_at = time.monotonic()
        self._set_stop_mode(True)
        self._schedule("reply_tick", 1000, self._reply_tick)

    def _reply_tick(self):
        """Show that Nova is still working (and for how long) without blocking anything."""
        if not self.busy or not self._stop_mode:
            return
        elapsed = time.monotonic() - self._reply_began
        if self._cancel_reply.is_set():
            self.set_status("Stopping…")
        elif not self._reply_started:
            self.set_status(f"Waiting for the model… {elapsed:.0f} s")
            if elapsed >= 8 and not self._slow_hint_shown:
                self._slow_hint_shown = True
                self.append_message("system", "The first reply after a pause can take a few seconds while Ollama "
                                              "loads the model into memory. Press Esc or the stop button to cancel.")
        else:
            self.set_status(f"Writing… {elapsed:.0f} s")
        self._schedule("reply_tick", 1000, self._reply_tick)

    def _finish_reply_progress(self):
        self._jobs.pop("reply_tick", None)
        self._set_stop_mode(False)

    def _on_typing(self, _event=None):
        if self.settings.get("model_warmup", "typing") == "typing":
            self._maybe_warm_model()

    def _maybe_warm_model(self):
        """Load the model in Ollama ahead of the first message, so the reply starts quickly.

        Runs at most once every 4 minutes, never while a reply is running, and
        only asks Ollama to load the model (an empty prompt generates nothing).
        """
        if self.busy or self._warming or self._exiting or time.monotonic() - self._model_used_at < 240:
            return
        self._warming = True
        self._model_used_at = time.monotonic()
        model = active_model()

        def worker():
            started = time.perf_counter()
            try:
                ollama.generate(model=model, prompt="",
                                options={"num_ctx": context_budget(model, self.settings).num_ctx})
                nova_perf.record("model warm-up (loading it before you send)", (time.perf_counter() - started) * 1000)
                log.info("Model %s warmed up in %.1f s", model, time.perf_counter() - started)
            except Exception as exc:
                log.info("Model warm-up skipped: %s", exc)
            finally:
                self._warming = False

        threading.Thread(target=worker, daemon=True, name="NovaModelWarmup").start()

    def _prewarm_imports(self):
        """Import the heavy libraries in the background once the window is up,
        so the first chat or recording doesn't wait for them."""
        def worker():
            started = time.perf_counter()
            for module in (ollama, backend.np, backend.wavfile, backend.sd):
                try:
                    module.available()
                except Exception:
                    pass
            nova_perf.record("startup: background imports", (time.perf_counter() - started) * 1000)

        threading.Thread(target=worker, daemon=True, name="NovaPrewarm").start()

    # -------------------- chat paging --------------------
    def _show_earlier(self):
        first = self._chat_first_id
        more = CHAT_PAGE * 2
        if self._chat_latest:
            self.load_chat(limit=self._chat_limit + more)
        else:
            self.load_chat(anchor_id=message_id_before(self.session_id, first, more), limit=self._chat_limit + more)
        if first is not None:
            try:
                self.root.update_idletasks()
                self.chat.text.yview(f"msg_{first}")
            except tk.TclError:
                pass

    # -------------------- reliability: timers, workers, shutdown --------------------
    def _schedule(self, name, delay_ms, callback):
        """Run callback after delay_ms. One pending job per name, so loops can't multiply,
        and every job can be cancelled at shutdown."""
        if self._exiting:
            return
        previous = self._jobs.pop(name, None)
        if previous:
            try:
                self.root.after_cancel(previous)
            except tk.TclError:
                pass

        def run():
            self._jobs.pop(name, None)
            callback()

        try:
            self._jobs[name] = self.root.after(delay_ms, run)
        except tk.TclError:
            pass

    def _start_wake_thread(self, attempts=40):
        """Start the wake-word listener, waiting for a previous one to release the
        microphone first, so two listeners can never run at once."""
        if not self.wake_active or self._exiting:
            return
        if self.wake_listener_running():
            if attempts <= 0:
                self.set_status("The previous Hey Nova listener didn't stop; try again")
                self.wake_active = False
                self._sync_wake_switch()
                return
            self.set_status("Waiting for the microphone…")
            self._schedule("wake_start", 250, lambda: self._start_wake_thread(attempts - 1))
            return
        self.wake_stop_event.clear()
        self.wake_resume_event.clear()
        self.wake_thread = threading.Thread(target=self._wake_worker, daemon=True, name="NovaWakeWord")
        self.wake_thread.start()

    def _auto_backup(self):
        """Once a day, in the background: keep the last 7 automatic backups."""
        if self._backup_running or self._exiting:
            return
        self._backup_running = True

        def worker():
            try:
                path = nova_backup.auto_backup(backend.DB_PATH, BACKUP_DIR, SETTINGS_PATH)
                if path:
                    log.info("Automatic backup created: %s", path.name)
            except Exception as exc:
                log.warning("Automatic backup failed: %s", exc)
            finally:
                self._backup_running = False

        threading.Thread(target=worker, daemon=True, name="NovaAutoBackup").start()

    def _shutdown(self):
        """Stop everything Nova started, in order, waiting briefly for each."""
        log.info("Shutting down")
        for name, job in list(self._jobs.items()):
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._jobs.clear()
        self._cancel_reply.set()
        self._summary_cancel.set()
        self.indexer.stop()
        task_ui = getattr(self, "_task_ui", None)
        if task_ui:
            task_ui["state"]["cancel"].set()
        self.wake_active = False
        self.wake_stop_event.set()
        self.wake_resume_event.set()
        self.voice_stop_event.set()
        SPEAKER.stop()
        close_ollama_connections()
        if getattr(self, "_http", None) is not None:
            try:
                self._http.close()          # ends any online request still waiting
            except Exception:
                pass
        deadline = time.monotonic() + 2.0
        self.indexer.join(max(0.0, deadline - time.monotonic()))
        for thread in (self.wake_thread, self.voice_thread):
            if thread is not None and thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    log.warning("%s didn't stop within 2 seconds", thread.name)
        for window in getattr(self, "_alert_windows", []):
            try:
                window.destroy()
            except tk.TclError:
                pass
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception as exc:
            log.debug("Tray icon stop failed: %s", exc)
        self.tray_icon = None
        try:
            with_db(lambda con: con.execute("PRAGMA optimize"))   # cheap; keeps query plans good over time
        except Exception as exc:
            log.debug("PRAGMA optimize skipped: %s", exc)

    def _restart_app(self):
        """Start a fresh copy of Nova (fixed interpreter and script), then close this one."""
        script = Path(__file__).resolve()
        interpreter = Path(sys.executable)
        windowless = interpreter.with_name("pythonw.exe")
        try:
            subprocess.Popen([str(windowless if windowless.exists() else interpreter), str(script)],
                             cwd=str(script.parent), close_fds=True)
        except OSError as exc:
            log.error("Couldn't restart Nova: %s", exc)
            messagebox.showinfo("Restart Nova", f"Please start Nova again yourself ({exc}).", parent=self.root)
        self.exit_application(force=True)

    # -------------------- Settings › Backup & restore --------------------
    def _build_backup_tab(self, win, tab):
        f = self.fonts
        ui_queue = queue.Queue()

        def pump():
            try:
                while True:
                    ui_queue.get_nowait()()
            except queue.Empty:
                pass
            except tk.TclError:
                return
            except Exception:
                log.exception("Backup tab update failed")
            try:
                if win.winfo_exists():
                    win.after(100, pump)
            except tk.TclError:
                pass

        card = ctk.CTkFrame(tab, fg_color=SURFACE, corner_radius=14)
        card.pack(fill="x", pady=(8, 0), padx=(0, 8))
        ctk.CTkLabel(card, text="Backups", font=f.ui(14, semibold=True), text_color=TEXT, anchor="w").pack(
            fill="x", padx=18, pady=(16, 2))
        ctk.CTkLabel(
            card,
            text=("A backup holds your chats, memories, notes, reminders, document index, task history and settings. "
                  "Nova also makes one automatically each day (keeping the last 7) in the backups folder next to "
                  "Nova. Backups stay on this PC unless you copy them somewhere else."),
            font=f.ui(12), text_color=MUTED, anchor="w", justify="left", wraplength=580,
        ).pack(fill="x", padx=18)
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(12, 4))
        status = ctk.CTkLabel(card, text="", font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                              wraplength=580)
        status.pack(fill="x", padx=18, pady=(4, 16))
        listing = ctk.CTkFrame(tab, fg_color="transparent")
        listing.pack(fill="x", pady=(10, 0), padx=(0, 8))
        controls = []        # the fixed buttons
        row_buttons = []     # "Restore…" buttons of the current list (rebuilt on refresh)

        def busy(on, message="", color=MUTED):
            for button in controls + row_buttons:
                try:
                    if button.winfo_exists():
                        button.configure(state="disabled" if on else "normal")
                except tk.TclError:
                    pass
            status.configure(text=message, text_color=WARNING if on else color)

        def background(work, done):
            def worker():
                try:
                    result, error = work(), None
                except nova_backup.BackupError as exc:
                    result, error = None, str(exc)
                except Exception as exc:
                    log.exception("Backup operation failed")
                    result, error = None, f"{type(exc).__name__}: {exc}"
                ui_queue.put(lambda: done(result, error))
            threading.Thread(target=worker, daemon=True, name="NovaBackup").start()

        def refresh():
            for child in listing.winfo_children():
                child.destroy()
            row_buttons.clear()
            items = nova_backup.list_backups(BACKUP_DIR)
            ctk.CTkLabel(listing, text=f"In the backups folder ({len(items)})", font=f.ui(13, semibold=True),
                         text_color=MUTED, anchor="w").pack(fill="x", padx=4, pady=(4, 2))
            if not items:
                ctk.CTkLabel(listing, text="No backups yet.", font=f.ui(12), text_color=SUBTLE, anchor="w").pack(
                    fill="x", padx=4)
            for path, when, size in items[:12]:
                row = ctk.CTkFrame(listing, fg_color=SURFACE, corner_radius=10)
                row.pack(fill="x", pady=2)
                kind = ("automatic" if path.name.startswith(nova_backup.AUTO_PREFIX) else
                        "before a restore" if path.name.startswith("pre-restore-") else "manual")
                ctk.CTkLabel(row, text=f"{when:%Y-%m-%d %H:%M} · {kind} · {size / 1024 / 1024:.1f} MB",
                             font=f.ui(13), text_color=TEXT, anchor="w").pack(side="left", padx=14, pady=8)
                restore = ctk.CTkButton(row, text="Restore…", width=90, height=28, corner_radius=8, font=f.ui(12),
                                        fg_color=SURFACE_2, hover_color=BORDER, text_color=TEXT,
                                        command=lambda p=path: restore_from(p))
                restore.pack(side="right", padx=10)
                row_buttons.append(restore)

        def back_up(destination):
            busy(True, "Backing up…")
            background(lambda: nova_backup.create_backup(backend.DB_PATH, destination, SETTINGS_PATH),
                       lambda path, error: (busy(False, f"Backup saved: {path}" if path else f"Backup failed: {error}",
                                                 SUCCESS if path else DANGER), refresh()))

        def save_as():
            chosen = filedialog.asksaveasfilename(
                parent=win, title="Save a Nova backup", defaultextension=nova_backup.SUFFIX,
                initialfile=nova_backup.default_name(), filetypes=[("Nova backup", f"*{nova_backup.SUFFIX}")])
            if chosen:
                path = Path(chosen)
                back_up(path if path.suffix.lower() == nova_backup.SUFFIX else path.with_name(path.name + nova_backup.SUFFIX))

        def restore_from(path=None):
            if path is None:
                chosen = filedialog.askopenfilename(parent=win, title="Restore a Nova backup",
                                                    filetypes=[("Nova backup", f"*{nova_backup.SUFFIX}")])
                if not chosen:
                    return
                path = Path(chosen)
            if self.busy or self.task_runner.busy:
                busy(False, "Wait for Nova to finish its current reply or task, then try again.", WARNING)
                return
            busy(True, "Checking the backup…")
            background(lambda: nova_backup.inspect_backup(path), lambda info, error: confirm(path, info, error))

        def describe_counts(counts):
            labels = {"sessions": "chats", "messages": "messages", "memories": "memories", "nova_notes": "notes",
                      "nova_reminders": "reminders", "doc_files": "documents", "task_runs": "tasks",
                          "nova_projects": "projects"}
            return ", ".join(f"{n} {labels.get(t, t)}" for t, n in counts.items()) or "nothing"

        def confirm(path, info, error):
            if error:
                busy(False, f"This backup can't be restored: {error}", DANGER)
                return
            try:
                current = describe_counts(with_db(nova_backup.table_counts))
            except Exception:
                current = "unknown (the current database couldn't be read)"
            question = (
                f"Restore the backup from {info.created_at[:16].replace('T', ' ')}?\n\n"
                f"Backup: {describe_counts(info.counts)}{' + settings' if info.includes_settings else ''}.\n"
                f"Now: {current}.\n\n"
                "This replaces ALL of Nova's current data. Your current data is saved as a backup first, so you "
                "can undo this. Nova restarts afterwards."
            )
            if not messagebox.askyesno("Restore backup", question, icon="warning", parent=win):
                busy(False, "Restore cancelled. Nothing was changed.")
                return
            self._maintenance = True
            self.indexer.stop()
            busy(True, "Restoring… don't close Nova.")
            background(lambda: nova_backup.restore_backup(path, backend.DB_PATH, BACKUP_DIR, SETTINGS_PATH,
                                                          confirmed=True), finished)

        def finished(result, error):
            if error:
                self._maintenance = False
                busy(False, f"Restore failed; your data wasn't replaced: {error}", DANGER)
                log.error("Restore failed: %s", error)
                return
            saved = (f"Your previous data was saved as {result.safety_backup.name}." if result.safety_backup else
                     f"Your previous (damaged) database was kept as {result.damaged_copy.name}." if result.damaged_copy
                     else "")
            messagebox.showinfo("Backup restored", f"The backup was restored. {saved}\n\nNova will now restart.",
                                parent=win)
            self._restart_app()

        def open_folder():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            try:
                os.startfile(str(BACKUP_DIR))  # Nova's own fixed folder
            except OSError as exc:
                busy(False, f"Couldn't open the folder: {exc}", DANGER)

        for text, command, kind in (("Back up now", lambda: back_up(BACKUP_DIR), "primary"),
                                    ("Save backup as…", save_as, "secondary"),
                                    ("Restore from file…", lambda: restore_from(None), "secondary"),
                                    ("Open folder", open_folder, "secondary")):
            button = self._button(buttons, text, command, kind=kind, width=10)
            button.pack(side="left", padx=(0, 8))
            controls.append(button)
        refresh()
        pump()

    # -------------------- Settings › Diagnostics --------------------
    def _build_diagnostics_tab(self, win, tab):
        f = self.fonts
        ui_queue = queue.Queue()
        state = {"checks": []}
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 4))
        status = ctk.CTkLabel(bar, text="", font=f.ui(12), text_color=MUTED, anchor="e")
        results = self._scroll_list(tab)
        results.pack(fill="both", expand=True)
        icons = {nova_diagnostics.OK: ("✓", SUCCESS), nova_diagnostics.WARNING: ("!", WARNING),
                 nova_diagnostics.ERROR: ("✗", DANGER), nova_diagnostics.INFO: ("·", MUTED)}

        def render(checks):
            state["checks"] = checks
            for child in results.winfo_children():
                child.destroy()
            area = None
            for check in checks:
                if check.area != area:
                    area = check.area
                    ctk.CTkLabel(results, text=area, font=f.ui(13, semibold=True), text_color=MUTED,
                                 anchor="w").pack(fill="x", padx=4, pady=(10, 2))
                row = ctk.CTkFrame(results, fg_color=SURFACE, corner_radius=8)
                row.pack(fill="x", pady=1, padx=(0, 8))
                glyph, color = icons.get(check.status, ("·", MUTED))
                ctk.CTkLabel(row, text=glyph, font=f.ui(14, semibold=True), text_color=color, width=24).pack(
                    side="left", padx=(10, 4), pady=6)
                ctk.CTkLabel(row, text=check.name, font=f.ui(13, semibold=True), text_color=TEXT, width=170,
                             anchor="w").pack(side="left")
                ctk.CTkLabel(row, text=check.detail, font=f.ui(12), text_color=MUTED, anchor="w", justify="left",
                             wraplength=360).pack(side="left", fill="x", expand=True, padx=(4, 10), pady=6)
            problems = sum(c.status in (nova_diagnostics.ERROR, nova_diagnostics.WARNING) for c in checks)
            status.configure(text=f"{problems} item{'s' if problems != 1 else ''} need attention" if problems
                             else "Everything looks good", text_color=WARNING if problems else SUCCESS)

        def run():
            status.configure(text="Checking…", text_color=MUTED)

            def worker():
                try:
                    checks = nova_diagnostics.run_checks(
                        backend.DB_PATH, active_model(), BACKUP_DIR, nova_settings.LOG_PATH,
                        settings_warnings=self._settings_warnings,
                        list_models=lambda: nova_settings.list_ollama_models(timeout=4),
                        find_whisper=backend.find_whisper, find_vosk_model=find_vosk_model,
                        count_input_devices=lambda: len(nova_settings.list_input_devices()),
                        performance=nova_perf.summary(),
                        online=nova_online.status_rows(self.settings, self.secrets),
                    )
                except Exception as exc:
                    log.exception("Diagnostics failed")
                    checks = [nova_diagnostics.Check("Diagnostics", "Run", nova_diagnostics.ERROR, str(exc))]
                ui_queue.put(lambda: render(checks))

            threading.Thread(target=worker, daemon=True, name="NovaDiagnostics").start()

        def copy_report():
            if state["checks"]:
                self.root.clipboard_clear()
                self.root.clipboard_append(nova_diagnostics.report_text(state["checks"]))
                status.configure(text="Report copied (contains no chat content or secrets)", text_color=SUCCESS)

        def open_logs():
            try:
                os.startfile(str(nova_settings.LOG_PATH.parent))  # Nova's own folder
            except OSError as exc:
                status.configure(text=f"Couldn't open the folder: {exc}", text_color=DANGER)

        def pump():
            try:
                while True:
                    ui_queue.get_nowait()()
            except queue.Empty:
                pass
            except tk.TclError:
                return
            try:
                if win.winfo_exists():
                    win.after(100, pump)
            except tk.TclError:
                pass

        for text, command in (("Run checks", run), ("Copy report", copy_report), ("Open log folder", open_logs)):
            self._button(bar, text, command, kind="secondary", width=10).pack(side="left", padx=(0, 8))
        status.pack(side="right", fill="x", expand=True)
        log_var = tk.BooleanVar(value=bool(self.settings.get("log_performance")))

        def toggle_logging():
            nova_perf.LOG_TIMINGS = bool(log_var.get())
            self._persist_setting("log_performance", nova_perf.LOG_TIMINGS)

        ctk.CTkSwitch(tab, text="Also write performance timings to nova.log", variable=log_var, onvalue=True,
                      offvalue=False, command=toggle_logging, font=f.ui(12), text_color=MUTED, progress_color=ACCENT,
                      fg_color=SURFACE_2, button_color=TEXT, switch_width=34, switch_height=18).pack(
            anchor="w", padx=4, pady=(0, 4), after=bar)   # (a scrollable frame can't be a pack anchor)
        pump()
        run()

    # -------------------- exit --------------------
    def exit_application(self, force=False):
        if self._exiting:
            return
        if not force and self.busy and not messagebox.askyesno(
                "Exit Nova", "A task is still running. Exit anyway?", parent=self.root):
            return
        try:
            active = len(with_db(nova_reminders.active))
        except Exception:
            active = 0
        if active and not force:
            tray_tip = ("\n\nTip: close the window instead to keep Nova running in the tray."
                        if self.tray_dependencies and self.settings.get("minimize_to_tray", True) else "")
            if not messagebox.askyesno(
                "Exit Nova",
                f"You have {active} active reminder{'s' if active != 1 else ''}. Nova can't alert you while it's "
                "closed (or while Windows is off); anything that comes due will be shown as missed when Nova "
                f"starts again.{tray_tip}\n\nExit anyway?",
                parent=self.root,
            ):
                return
        self._exiting = True
        try:
            self._shutdown()
        except Exception:
            log.exception("Error during shutdown")
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def close(self):
        if self.settings.get("minimize_to_tray", True) and self.tray_dependencies and self.tray_icon:
            self.hide_to_tray()
            return
        self.exit_application()


def startup_database_check():
    """Check the database before Nova opens it. Returns True to continue.

    A damaged database is never deleted or overwritten without asking: the
    user can restore a backup (the damaged file is kept beside it), move the
    damaged file aside and start fresh, or quit with nothing changed.
    """
    db_path = Path(backend.DB_PATH)
    healthy, message, version = nova_backup.check_database(db_path)
    if healthy and version <= nova_backup.SCHEMA_VERSION:
        return True
    log.error("Database check at startup: %s", message)
    root = tk.Tk()
    root.withdraw()
    try:
        if healthy:  # made by a newer Nova
            return messagebox.askyesno(
                "Nova", f"{message}\n\nUsing it with this older version may not work correctly. Continue anyway?",
                icon="warning", parent=root)
        backups = nova_backup.list_backups(BACKUP_DIR)
        if backups:
            newest = backups[0]
            answer = messagebox.askyesnocancel(
                "Nova can't open its database",
                f"{message}\n\nYes: restore the newest backup ({newest[1]:%Y-%m-%d %H:%M}). The damaged file is kept.\n"
                "No: choose a backup file.\nCancel: quit without changing anything.",
                icon="error", parent=root)
            if answer is None:
                return False
            path = newest[0] if answer else filedialog.askopenfilename(
                parent=root, title="Choose a Nova backup", filetypes=[("Nova backup", f"*{nova_backup.SUFFIX}")])
            if not path:
                return False
            try:
                result = nova_backup.restore_backup(path, db_path, BACKUP_DIR, SETTINGS_PATH, confirmed=True)
            except nova_backup.BackupError as exc:
                messagebox.showerror("Nova", f"That backup couldn't be restored: {exc}\n\nNothing was changed.",
                                     parent=root)
                return False
            kept = f" The damaged database was kept as {result.damaged_copy.name}." if result.damaged_copy else ""
            messagebox.showinfo("Nova", f"Restored the backup from {result.info.created_at[:16].replace('T', ' ')}.{kept}",
                                parent=root)
            return True
        if messagebox.askyesno(
            "Nova can't open its database",
            f"{message}\n\nThere are no backups to restore. Move the damaged file aside (it is kept, not deleted) "
            "and start with an empty database?",
            icon="error", parent=root,
        ):
            moved = nova_backup.set_aside_damaged(db_path)
            messagebox.showinfo("Nova", f"The damaged database was kept as {moved.name}. Nova will start with an "
                                        "empty database.", parent=root)
            return True
        return False
    finally:
        root.destroy()


def main():
    nova_settings.setup_logging()
    log.info("Nova %s starting (Python %s)", NOVA_VERSION, sys.version.split()[0])
    if not startup_database_check():   # before any theming, with its own temporary window
        log.info("Nova closed at the database check")
        return
    ctk.set_appearance_mode("dark")
    try:
        initialize_database()
        initialize_productivity_tables()
    except sqlite3.Error as exc:
        log.exception("Database setup failed")
        _fatal(f"Nova couldn't prepare its database: {exc}\n\nDetails are in {nova_settings.LOG_PATH.name}. "
               "If this keeps happening, restore a backup from Settings › Backup & restore.")
    try:
        initialize_task_tables()
    except sqlite3.Error as exc:
        log.error("Couldn't prepare task history: %s", exc)
    try:
        if not initialize_document_tables():
            log.warning("Document search will use keyword matching (SQLite FTS5 unavailable)")
    except sqlite3.Error as exc:
        log.error("Couldn't prepare the document index: %s", exc)
    try:
        initialize_context_tables()
    except sqlite3.Error as exc:
        log.error("Couldn't prepare projects and chat summaries: %s", exc)
    try:
        with_db(nova_backup.stamp_schema_version)
    except sqlite3.Error as exc:
        log.warning("Couldn't record the schema version: %s", exc)
    root = ctk.CTk()

    def report_callback_exception(exc_type, exc, tb):
        # Under pythonw.exe there is no console, so Tk errors would vanish.
        log.error("Unhandled error in the interface", exc_info=(exc_type, exc, tb))
        try:
            messagebox.showerror("Nova", f"Something went wrong: {exc}\n\nDetails are in {nova_settings.LOG_PATH.name}.",
                                 parent=root)
        except tk.TclError:
            pass

    root.report_callback_exception = report_callback_exception
    NovaGUI(root)
    root.mainloop()
    log.info("Nova closed")


if __name__ == "__main__":
    main()

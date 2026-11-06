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
from datetime import datetime, timedelta, timezone
import re
import shutil
import webbrowser
import json
import queue
import threading
import subprocess
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

try:
    import assistant as backend
except Exception as exc:
    raise SystemExit(
        "Could not import assistant.py. Keep nova_gui.py in the same folder "
        "as assistant.py and check that the project's virtual environment is active.\n\n"
        f"Details: {exc}"
    )

try:
    import ollama
except Exception as exc:
    raise SystemExit(f"Missing Ollama Python package. Install it in your venv: pip install ollama\n{exc}")

try:
    import customtkinter as ctk
except Exception as exc:
    raise SystemExit(f"Missing CustomTkinter. Install it in your venv: pip install customtkinter\n{exc}")

from nova_widgets import (
    ACCENT, ACCENT_HOVER, ACCENT_SOFT, BG, BORDER, DANGER, DANGER_SOFT, MUTED,
    SIDEBAR, SUBTLE, SUCCESS, SURFACE, SURFACE_2, TEXT, WARNING,
    ChatView, Fonts, Icon, NavButton, SessionList, StatusPill, Tooltip,
    autohide_scrollbar, bind_tree, fit_text, icon_button, set_cursor,
)

APP_TITLE = "Nova"
MODEL = getattr(backend, "MODEL", "gemma3:4b")


SETTINGS_PATH = Path(__file__).resolve().with_name("nova_settings.json")
STARTUP_SCRIPT_NAME = "NovaAI.vbs"


def load_nova_settings():
    defaults = {"minimize_to_tray": True}
    try:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update(data)
    except Exception:
        pass
    return defaults


def save_nova_settings(settings):
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


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
        path.write_text(vbs, encoding="utf-8")
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


def fetch_sessions():
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


def fetch_messages(session_id):
    """Return the full (role, content) history of a conversation, oldest first.

    Don't use backend.load_context() here: it is capped at MAX_CONTEXT_MESSAGES
    for the model's context window, which would truncate the displayed chat.
    """
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("PRAGMA table_info(messages)")
        cols = {r[1] for r in cur.fetchall()}
        session_col = "session_id" if "session_id" in cols else None
        role_col = "role" if "role" in cols else None
        content_col = "content" if "content" in cols else None
        order_col = "id" if "id" in cols else ("created_at" if "created_at" in cols else None)
        if not all((session_col, role_col, content_col)):
            return []
        query = f'SELECT "{role_col}", "{content_col}" FROM messages WHERE "{session_col}" = ?'
        if order_col:
            query += f' ORDER BY "{order_col}" ASC'
        cur.execute(query, (session_id,))
        return [(r[0], r[1]) for r in cur.fetchall() if r[0] in ("user", "assistant")]
    finally:
        con.close()


def get_memories():
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, category, content, updated_at FROM memories "
            "ORDER BY updated_at DESC, id DESC"
        )
        return cur.fetchall()
    except Exception:
        return []
    finally:
        con.close()


def add_memory_db(category, content):
    """Save via the backend so GUI and CLI memories share one format.

    Returns True if a new memory was created, False if an identical one was
    refreshed. Raises ValueError for invalid input.
    """
    _memory_id, _category, created = backend.save_memory(content, category)
    return created


def delete_memory_db(memory_id):
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        con.commit()
        return cur.rowcount > 0
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
    finally:
        con.close()


def list_notes_db():
    con = get_db_connection()
    try:
        return con.execute(
            "SELECT id, title, content, created_at, updated_at "
            "FROM nova_notes ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    finally:
        con.close()


def save_note_db(title, content, note_id=None):
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
        con.commit()
        return cur.lastrowid if note_id is None else note_id
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
    con = get_db_connection()
    try:
        cur = con.execute(
            "INSERT INTO nova_reminders (title, due_at, completed, created_at) "
            "VALUES (?, ?, 0, ?)",
            (title.strip(), due_at, local_timestamp()),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def complete_reminder_db(reminder_id):
    con = get_db_connection()
    try:
        cur = con.execute(
            "UPDATE nova_reminders SET completed = 1 WHERE id = ?",
            (reminder_id,),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_reminder_db(reminder_id):
    con = get_db_connection()
    try:
        cur = con.execute("DELETE FROM nova_reminders WHERE id = ?", (reminder_id,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def claim_due_reminders_db(now_text):
    """Atomically mark due reminders as notified so they cannot pop up repeatedly."""
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, title, due_at FROM nova_reminders "
            "WHERE completed = 0 AND notified_at IS NULL AND due_at <= ? "
            "ORDER BY due_at ASC, id ASC",
            (now_text,),
        )
        due = cur.fetchall()
        if due:
            ids = [row[0] for row in due]
            placeholders = ",".join("?" for _ in ids)
            cur.execute(
                f"UPDATE nova_reminders SET notified_at = ? "
                f"WHERE id IN ({placeholders})",
                [local_timestamp(), *ids],
            )
            con.commit()
        return due
    finally:
        con.close()



def handle_productivity_command(text):
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

    # List pending reminders.
    if re.fullmatch(r"(show|list|view|check)\s+(my\s+)?(pending\s+)?reminders", lowered):
        rows = list_reminders_db()
        if not rows:
            return "You have no pending reminders."
        lines = ["Your pending reminders:"]
        for row in rows[:15]:
            lines.append(f"#{row[0]} — {row[1]} (due {row[2]})")
        if len(rows) > 15:
            lines.append(f"…and {len(rows) - 15} more.")
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
        note_id = save_note_db(title, content)
        return f"Saved note #{note_id}: {title}"

    # Reminders using a relative delay: "remind me to call home in 20 minutes".
    relative_match = re.match(
        r"^(?:nova[, ]+)?remind\s+me\s+(?:to\s+)?(.+?)\s+in\s+(\d+)\s+(minutes?|mins?|hours?|hrs?|days?)\s*[.!]?$",
        raw,
        flags=re.IGNORECASE,
    )
    if relative_match:
        title = relative_match.group(1).strip(" .")
        amount = int(relative_match.group(2))
        unit = relative_match.group(3).lower()
        if not title:
            return "Tell me what to remind you about."
        if amount < 1:
            return "The reminder delay must be at least 1 minute."
        if amount > 365 * 24 * 60:
            return "Please choose a reminder within the next year."
        if unit.startswith(("min",)):
            due = datetime.now() + timedelta(minutes=amount)
        elif unit.startswith(("hour", "hr")):
            due = datetime.now() + timedelta(hours=amount)
        else:
            due = datetime.now() + timedelta(days=amount)
        due = due.replace(second=0, microsecond=0)
        reminder_id = save_reminder_db(title, due.strftime("%Y-%m-%d %H:%M"))
        return f"Reminder #{reminder_id} saved: {title}\nDue: {due.strftime('%Y-%m-%d %H:%M')} (your PC's local time)."

    # Explicit date/time: "remind me to submit report at 2026-11-07 09:30".
    explicit_match = re.match(
        r"^(?:nova[, ]+)?remind\s+me\s+(?:to\s+)?(.+?)\s+(?:at|on)\s+"
        r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})\s*[.!]?$",
        raw,
        flags=re.IGNORECASE,
    )
    if explicit_match:
        title = explicit_match.group(1).strip(" .")
        try:
            due = datetime.strptime(
                f"{explicit_match.group(2)} {explicit_match.group(3)}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return "I couldn't read that date/time. Use YYYY-MM-DD HH:MM, e.g. 2026-11-07 09:30."
        if due <= datetime.now():
            return "That time is in the past. Please provide a future date and time."
        reminder_id = save_reminder_db(title, due.strftime("%Y-%m-%d %H:%M"))
        return f"Reminder #{reminder_id} saved: {title}\nDue: {due.strftime('%Y-%m-%d %H:%M')} (your PC's local time)."

    # Catch reminder intent with unsupported date wording instead of silently passing it to the LLM.
    if re.match(r"^(?:nova[, ]+)?remind\s+me\b", lowered):
        return (
            "I can create reminders using either format:\n"
            "• Remind me to call home in 20 minutes\n"
            "• Remind me to submit the report at 2026-11-07 09:30\n"
            "Times use your PC's local clock. Nova must be running to show the alert."
        )

    return None


def memory_prompt(user_text=""):
    """Return a compact, relevance-ranked set of saved memories for this request."""
    try:
        rows = get_memories()
        if not rows:
            return ""

        # Prefer memories whose category/content overlap with the current request.
        stop_words = {
            "this", "that", "with", "from", "have", "your", "about", "what",
            "when", "where", "which", "would", "could", "should", "please",
            "tell", "help", "want", "need", "does", "doing", "been", "were",
            "them", "they", "then", "than", "into", "also", "just", "like",
            "make", "give", "show", "some", "much", "more", "very", "here",
        }
        def terms(value):
            return {
                token for token in re.findall(r"[a-zA-Z0-9_+#.-]{2,}", str(value).lower())
                if token not in stop_words
            }

        query_terms = terms(user_text)
        scored = []
        for row in rows:
            memory_id, category, content = row[0], row[1], row[2]
            memory_terms = terms(f"{category} {content}")
            overlap = len(query_terms & memory_terms)
            # Recent memories are a tie-breaker; general preferences remain useful.
            general_bonus = 1 if str(category).lower() in {
                "preference", "preferences", "profile", "general"
            } else 0
            score = overlap * 3 + general_bonus
            scored.append((score, row))
        scored.sort(key=lambda item: (item[0], item[1][0]), reverse=True)

        if query_terms:
            relevant = [row for score, row in scored if score > 0][:12]
            # If no memory matches the request, include a small recent baseline.
            if not relevant:
                relevant = [row for _, row in scored[:5]]
        else:
            relevant = [row for _, row in scored[:8]]

        lines = [
            "Long-term memory (user-saved facts; use only when relevant and do not invent details):"
        ]
        for row in relevant:
            lines.append(f"- [{row[1]}] {row[2]}")
        return "\n".join(lines)
    except Exception:
        return ""


def system_prompt(user_text=""):
    base = getattr(
        backend,
        "SYSTEM_PROMPT",
        "You are Nova, a helpful local AI assistant. Be clear, accurate, and concise.",
    )
    memories = memory_prompt(user_text)
    return str(base) + (("\n\n" + memories) if memories else "")


def save_message_safe(session_id, role, content):
    fn = getattr(backend, "save_message", None)
    if not callable(fn):
        raise RuntimeError("assistant.py does not expose save_message().")
    fn(session_id, role, content)


def record_and_transcribe(status_callback):
    """Record until silence, then transcribe locally; no console input.

    Uses the shared voice pipeline in assistant.py, so the CLI and GUI behave
    identically. Raises backend.VoiceInputError with a user-facing message.
    """
    audio = backend.record_until_silence(status_callback)
    return backend.transcribe_audio(audio, status_callback)



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
        matches = []
        needle = term.casefold()
        deadline = time.monotonic() + FILE_SEARCH_TIME_LIMIT
        timed_out = False
        # os.walk skips unreadable folders instead of aborting the whole search.
        for dirpath, dirnames, filenames in os.walk(search_root):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in FILE_SEARCH_SKIP_DIRS
            ]
            for name in filenames:
                if needle in name.casefold():
                    matches.append(Path(dirpath) / name)
                    if len(matches) >= 10:
                        break
            if len(matches) >= 10:
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
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


def detect_wake_phrase(status_callback, stop_event, resume_event, event_queue, is_busy=lambda: False):
    """Continuously listen locally for 'hey nova' using Vosk's constrained grammar.

    Detections while is_busy() is true (Nova is answering or recording) are
    ignored so two requests can't run at once.
    """
    try:
        import sounddevice as sd
        from vosk import Model, KaldiRecognizer
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

    status_callback("Loading local wake-word model…")
    model = Model(str(model_path))
    grammar = json.dumps(["hey nova", "hey no va", "[unk]"])
    status_callback("Wake word active • say “Hey Nova”")
    while not stop_event.is_set():
        resume_event.clear()
        recognizer = KaldiRecognizer(model, 16000, grammar)
        recognizer.SetWords(False)
        audio_queue = queue.Queue(maxsize=40)

        def callback(indata, frames, time_info, status):
            try:
                audio_queue.put_nowait(bytes(indata))
            except queue.Full:
                pass

        detected = False
        try:
            with sd.RawInputStream(
                samplerate=16000, blocksize=4096,
                device=getattr(backend, "MICROPHONE_DEVICE", 1),
                dtype="int16", channels=1, callback=callback,
            ):
                while not stop_event.is_set():
                    try:
                        data = audio_queue.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(data):
                        spoken = (json.loads(recognizer.Result()).get("text") or "").lower()
                        if "hey nova" in spoken or "hey no va" in spoken:
                            detected = True
                            break
        except Exception as exc:
            raise RuntimeError(f"Microphone/wake-word error: {exc}") from exc

        if stop_event.is_set():
            break
        if not detected:
            continue
        if is_busy():
            event_queue.put((
                "system",
                "Heard “Hey Nova”, but Nova is busy. Say it again when the current task finishes.",
            ))
            continue

        status_callback("Wake phrase detected • speak your command")
        try:
            transcript = record_and_transcribe(status_callback)
            event_queue.put(("wake_transcript_ready", transcript))
        except Exception as exc:
            event_queue.put(("wake_voice_error", str(exc)))
        while not stop_event.is_set() and not resume_event.wait(timeout=0.25):
            pass

    status_callback("Wake-word listener stopped")



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
        self.settings = load_nova_settings()
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
        self.session_id = current_session_id()
        self.sessions = []
        self.chat_cache = []
        self._build_ui()
        self.refresh_sessions(select_current=True)
        self.load_chat()
        self.refresh_memories()
        initialize_productivity_tables()
        self.set_status("Ready")
        self.root.after(100, self.poll_events)
        self.root.after(1000, self.poll_due_reminders)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.tray_dependencies:
            self.start_tray_icon()
        self.root.after(200, self.input_text.focus_set)

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
        self.session_list = SessionList(sidebar, f, on_select=self.select_session, on_delete=self.delete_chat)
        self.session_list.pack(fill="both", expand=True, padx=(4, 0))

        ctk.CTkFrame(sidebar, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x", padx=16, pady=8)
        NavButton(sidebar, f, Icon.MEMORY, "Memories", self.open_memories).pack(fill="x", padx=12, pady=1)
        NavButton(sidebar, f, Icon.NOTE, "Notes & reminders", self.open_productivity).pack(fill="x", padx=12, pady=1)
        NavButton(sidebar, f, Icon.SETTINGS, "Settings", self.open_startup_settings).pack(fill="x", padx=12, pady=1)
        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=(10, 16))
        ctk.CTkLabel(footer, text="●", font=ctk.CTkFont(family=f.symbol, size=9), text_color=SUCCESS, width=10).pack(side="left")
        ctk.CTkLabel(footer, text=f"Running locally · {MODEL}", font=f.ui(12), text_color=SUBTLE).pack(side="left", padx=(6, 0))

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
        Tooltip(self.send_button, "Send (Enter)", f)

        hints = ctk.CTkFrame(self.composer_wrap, fg_color="transparent")
        hints.pack(fill="x", pady=(9, 0), padx=8)
        self.wake_switch = ctk.CTkSwitch(
            hints, text="Hey Nova", font=f.ui(13), text_color=MUTED,
            command=self.toggle_wake_word, progress_color=ACCENT, fg_color=SURFACE_2,
            button_color=TEXT, button_hover_color="#ffffff", switch_width=34, switch_height=18,
        )
        self.wake_switch.pack(side="left")
        Tooltip(self.wake_switch, "Listen for “Hey Nova” — works offline", f)
        ctk.CTkLabel(hints, text="Enter to send  ·  Shift+Enter for a new line", font=f.ui(12), text_color=SUBTLE).pack(side="right")

        self.chat = ChatView(main, f, on_suggestion=self._use_suggestion)
        self.chat.pack(fill="both", expand=True)
        self.chat.bind("<Configure>", self._align_composer, add="+")

        self.input_text.bind("<Return>", self._enter_behavior)
        self.input_text.bind("<Control-Return>", self._send_shortcut)
        for sequence in ("<KeyRelease>", "<FocusIn>", "<FocusOut>", "<<Paste>>"):
            self.input_text.bind(sequence, lambda _e: self.root.after_idle(self._update_composer))
        self.root.after(50, self._update_composer)

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
        if role == "user":
            self.chat.add_user(content)
        elif role == "assistant":
            self.chat.add_assistant(content)
        else:
            self.chat.add_info(content)

    def clear_chat_view(self):
        self.chat.clear()

    def load_chat(self):
        items = fetch_messages(self.session_id)
        if not items:
            self.chat.show_welcome(self.SUGGESTIONS)
        else:
            self.chat.clear()
            for role, content in items:
                self.append_message(role, content)
            self.root.update_idletasks()
            self.chat.scroll_to_end()
        self._update_title()

    def _update_title(self):
        self._full_title = next(
            (label for sid, label, _date in self.sessions if sid == self.session_id),
            "New conversation",
        )
        self._fit_title()

    def _fit_title(self):
        """Fit the conversation title into the header space left of the status pill."""
        header = self.title_label.master
        available = header.winfo_width() - self.status_pill.winfo_width() - 28 - 12 - 44
        if available < 100:
            available = 600
        self.title_label.configure(text=fit_text(self._full_title, self._title_font, available))

    def refresh_sessions(self, select_current=False):
        self.sessions = fetch_sessions()
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
        if self.session_id is not None and not fetch_messages(self.session_id):
            self.load_chat()
            self.input_text.focus_set()
            return
        self.session_id = new_session_id()
        if self.session_id is None:
            messagebox.showerror("Nova", "Could not create a new conversation.", parent=self.root)
            return
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
        count = len(fetch_messages(session_id))
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
        try:
            try:
                action_result = handle_productivity_command(text)
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
        if speak:
            def speak_worker():
                try:
                    engine = call_backend("initialize_voice_engine", default=None)
                    if engine is not None:
                        call_backend("speak_response", engine, action_result)
                    else:
                        import pyttsx3
                        engine = pyttsx3.init()
                        engine.say(action_result)
                        engine.runAndWait()
                        engine.stop()
                except Exception:
                    pass
                finally:
                    if self.wake_active:
                        self.wake_resume_event.set()
            threading.Thread(target=speak_worker, daemon=True).start()
        elif self.wake_active:
            self.wake_resume_event.set()

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

    def toggle_wake_word(self):
        if self.wake_active:
            self.wake_active = False
            self.wake_stop_event.set()
            self.wake_resume_event.set()
            self._sync_wake_switch()
            self.set_controls_enabled(not self.busy)
            self.set_status("Stopping wake-word listener…")
            return
        if self.busy:
            self._sync_wake_switch()
            messagebox.showinfo("Hey Nova", "Wait for the current task to finish before enabling wake-word mode.", parent=self.root)
            return
        self.wake_active = True
        self.wake_stop_event.clear()
        self.wake_resume_event.clear()
        self._sync_wake_switch()
        self.set_controls_enabled(True)  # disables Voice while wake mode is on
        self.set_status("Starting wake-word listener…")
        self.events.put(("system", "Wake-word mode is on. Say “Hey Nova”, then speak your command."))
        self.wake_thread = threading.Thread(target=self._wake_worker, daemon=True)
        self.wake_thread.start()

    def _wake_worker(self):
        try:
            detect_wake_phrase(
                lambda status: self.events.put(("status", status)),
                self.wake_stop_event,
                self.wake_resume_event,
                self.events,
                is_busy=lambda: self.busy,
            )
        except Exception as exc:
            self.events.put(("wake_setup_error", str(exc)))

    def wake_listener_running(self):
        return self.wake_thread is not None and self.wake_thread.is_alive()

    def start_voice(self):
        if self.busy:
            return
        # The wake-word listener owns the microphone while active, and can keep
        # it for a few seconds after being switched off (e.g. mid-recording).
        if self.wake_active or self.wake_listener_running():
            self.set_status("Microphone in use by Hey Nova • turn it off or wait")
            return
        self.busy = True
        self.voice_response = True
        self.set_controls_enabled(False)
        self.set_status("Listening…")
        threading.Thread(target=self._voice_worker, daemon=True).start()

    def _voice_worker(self):
        try:
            transcript = record_and_transcribe(lambda s: self.events.put(("status", s)))
            self.events.put(("transcript_ready", transcript))
        except Exception as exc:
            self.events.put(("voice_error", str(exc)))

    # -------------------- model replies --------------------
    def begin_generation(self, user_text, speak=False):
        if self.busy:
            return
        self.busy = True
        self.voice_response = speak
        self.set_controls_enabled(False)
        self.set_status("Thinking…")
        self.events.put(("assistant_start", None))
        threading.Thread(target=self._generation_worker, args=(self.session_id, user_text, speak), daemon=True).start()

    def _generation_worker(self, session_id, user_text, speak):
        try:
            save_message_safe(session_id, "user", user_text)
            history = fetch_messages(session_id)
            # Keep a bounded context while ensuring the current user message is included.
            limit = int(getattr(backend, "MAX_CONTEXT_MESSAGES", 12))
            history = history[-max(2, limit):]
            # Like backend.load_context(): the context must start with a user turn.
            while history and history[0][0] != "user":
                history.pop(0)
            messages = [{"role": "system", "content": system_prompt(user_text)}]
            messages.extend({"role": role, "content": content} for role, content in history)
            response_parts = []
            stream = ollama.chat(
                model=MODEL,
                messages=messages,
                stream=True,
                options={"num_ctx": 4096},
            )
            for chunk in stream:
                piece = ""
                try:
                    piece = chunk["message"]["content"]
                except Exception:
                    try:
                        piece = chunk.message.content
                    except Exception:
                        pass
                if piece:
                    response_parts.append(piece)
                    self.events.put(("token", piece))
            answer = "".join(response_parts).strip()
            if not answer:
                answer = "I couldn't generate a response. Please try again."
            save_message_safe(session_id, "assistant", answer)
            if speak:
                try:
                    voice_engine = call_backend("initialize_voice_engine", default=None)
                    if voice_engine is not None:
                        call_backend("speak_response", voice_engine, answer)
                    else:
                        import pyttsx3
                        engine = pyttsx3.init()
                        engine.say(answer)
                        engine.runAndWait()
                        engine.stop()
                except Exception as exc:
                    self.events.put(("system", f"Response generated, but speech output failed: {exc}"))
            self.events.put(("generation_done", None))
        except Exception as exc:
            self.events.put(("generation_error", f"{exc}\n\nCheck that Ollama is running and model '{MODEL}' is installed."))

    def set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.send_button.configure(state=state, fg_color=ACCENT if enabled else SURFACE_2)
        # Voice and Hey Nova share the microphone; never allow both at once.
        self.voice_button.configure(
            state="normal" if enabled and not self.wake_active else "disabled"
        )
        self.input_text.configure(state=state)

    def poll_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    self.set_status(payload)
                elif event == "system":
                    self.append_message("system", payload)
                elif event == "assistant_start":
                    self.chat.begin_assistant()
                elif event == "token":
                    self.chat.stream(payload)
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
                    if self.busy:
                        self.append_message(
                            "system",
                            f"Heard “{payload}”, but Nova was busy, so it wasn't sent. "
                            "Say “Hey Nova” again when the current task finishes.",
                        )
                        self.wake_resume_event.set()
                        continue
                    self.set_status("Wake phrase heard • confirm command")
                    if messagebox.askyesno("Confirm voice input", f"Send this to Nova?\n\n{payload}", parent=self.root):
                        self.append_message("user", payload)
                        self.handle_user_message(payload, speak=True)
                    else:
                        self.set_status("Command cancelled • still listening for Hey Nova")
                        self.wake_resume_event.set()
                elif event == "wake_voice_error":
                    self.set_status("Wake-word voice capture failed")
                    messagebox.showerror("Hey Nova", payload, parent=self.root)
                    self.wake_resume_event.set()
                elif event == "wake_setup_error":
                    self.wake_active = False
                    self.wake_stop_event.set()
                    self._sync_wake_switch()
                    self.set_controls_enabled(not self.busy)
                    self.set_status("Wake-word setup failed")
                    messagebox.showerror("Hey Nova setup", payload, parent=self.root)
                elif event == "transcript_ready":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Transcript ready")
                    if messagebox.askyesno("Confirm voice input", f"Send this to Nova?\n\n{payload}", parent=self.root):
                        self.append_message("user", payload)
                        self.handle_user_message(payload, speak=True)
                    else:
                        self.set_status("Voice input cancelled")
                elif event == "voice_error":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Voice input failed")
                    messagebox.showerror("Voice input", payload, parent=self.root)
                elif event == "generation_done":
                    self.chat.end_assistant()
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Ready")
                    self.refresh_sessions(select_current=True)
                    if self.wake_active:
                        self.wake_resume_event.set()
                elif event == "generation_error":
                    self.chat.end_assistant()
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Error • check that Ollama is running")
                    self.chat.add_info(payload.split("\n")[0], error=True)
                    messagebox.showerror("Nova", payload, parent=self.root)
                    if self.wake_active:
                        self.wake_resume_event.set()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    # -------------------- dialogs: shared pieces --------------------
    def refresh_memories(self):
        try:
            self.memory_rows = get_memories()
        except Exception:
            self.memory_rows = []

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
    def open_memories(self):
        f = self.fonts
        win = self._dialog("Memories", "640x640", (520, 460))
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=22)
        self._dialog_header(
            body, "Memories",
            "Facts you save here are shared with Nova in future chats. Nothing is saved automatically.",
        )

        form = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14)
        form.pack(fill="x")
        category = self._entry(form, "Category", width=130)
        category.insert(0, "general")
        category.pack(side="left", padx=(12, 6), pady=12)
        content = self._entry(form, "e.g. I prefer short, practical answers")
        content.pack(side="left", fill="x", expand=True, padx=6, pady=12)

        items = self._scroll_list(body)
        items.pack(fill="both", expand=True, pady=(14, 0))

        def refresh():
            for child in items.winfo_children():
                child.destroy()
            rows = get_memories()
            if not rows:
                self._empty_state(items, Icon.MEMORY, "No memories yet",
                                  "Add facts about yourself above, like your name or how you like answers.")
                return
            for row in rows:
                memory_id, memory_category, memory_text, updated = row[0], row[1], row[2], row[3]
                card = ctk.CTkFrame(items, fg_color=SURFACE, corner_radius=12)
                card.pack(fill="x", pady=4, padx=(0, 8))
                top = ctk.CTkFrame(card, fg_color="transparent")
                top.pack(fill="x", padx=14, pady=(10, 0))
                self._chip(top, memory_category, ACCENT_SOFT, ACCENT).pack(side="left")
                ctk.CTkLabel(top, text=format_timestamp(updated), font=f.ui(12), text_color=SUBTLE).pack(side="left", padx=10)
                remove = icon_button(top, f, Icon.DELETE, lambda mid=memory_id: delete(mid),
                                     size=28, icon_size=12, hover=DANGER_SOFT, text_color=SUBTLE)
                remove.pack(side="right")
                Tooltip(remove, "Delete memory", f)
                ctk.CTkLabel(
                    card, text=memory_text, font=f.ui(14), text_color=TEXT,
                    anchor="w", justify="left", wraplength=500,
                ).pack(fill="x", padx=14, pady=(6, 12))

        def add(_event=None):
            text = content.get().strip()
            if not text:
                content.focus_set()
                return
            try:
                created = add_memory_db(category.get(), text)
                content.delete(0, "end")
                refresh()
                self.set_status("Memory saved" if created else "Memory already saved • moved to top")
            except ValueError as exc:
                messagebox.showwarning("Memories", str(exc), parent=win)
            except Exception as exc:
                messagebox.showerror("Memories", str(exc), parent=win)

        def delete(memory_id):
            if not messagebox.askyesno("Delete memory", "Delete this memory?", parent=win):
                return
            try:
                delete_memory_db(memory_id)
                refresh()
                self.set_status("Memory deleted")
            except Exception as exc:
                messagebox.showerror("Memories", str(exc), parent=win)

        self._button(form, "Save", add, width=80).pack(side="right", padx=(6, 12), pady=12)
        content.bind("<Return>", add)
        refresh()
        win.after(120, content.focus_set)

    # -------------------- Reminders (polling) --------------------
    def poll_due_reminders(self):
        """Check due reminders on Tk's main thread; persist notification state first."""
        try:
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
            due = claim_due_reminders_db(now_text)
            for reminder in due:
                reminder_id, title, due_at = reminder[0], reminder[1], reminder[2]
                self.set_status(f"Reminder: {title}")
                messagebox.showinfo(
                    "Nova reminder",
                    f"{title}\n\nScheduled for: {due_at}",
                    parent=self.root,
                )
        except Exception as exc:
            self.set_status(f"Reminder check failed: {exc}")
        finally:
            try:
                if self.root.winfo_exists():
                    self.root.after(1000, self.poll_due_reminders)
            except tk.TclError:
                pass

    # -------------------- Notes & reminders --------------------
    def open_productivity(self):
        """Open local Notes and Reminders manager."""
        f = self.fonts
        win = self._dialog("Notes & reminders", "960x660", (800, 540))
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
                ctk.CTkLabel(card, text=format_timestamp(row[4] or row[3]), font=f.ui(11),
                             text_color=SUBTLE, anchor="w").pack(fill="x", padx=14, pady=(0, 10))
                bind_tree(card, "<Button-1>", lambda _e, nid=note_id: load_note(nid))
                set_cursor(card)

        def new_note():
            selected["id"] = None
            title_entry.delete(0, "end")
            content_box.delete("1.0", "end")
            meta.configure(text="New note")
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
                selected["id"] = save_note_db(title, text, selected["id"])
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
        form = ctk.CTkFrame(reminders_tab, fg_color=SURFACE, corner_radius=14)
        form.pack(fill="x", pady=(8, 0))
        reminder_title = self._entry(form, "Remind me to…")
        reminder_title.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=12)
        reminder_due = self._entry(form, "YYYY-MM-DD HH:MM", width=170)
        default_due = (datetime.now() + timedelta(hours=1)).replace(second=0, microsecond=0)
        reminder_due.insert(0, default_due.strftime("%Y-%m-%d %H:%M"))
        reminder_due.pack(side="left", padx=6, pady=12)
        ctk.CTkLabel(
            reminders_tab,
            text="Time uses the 24-hour clock. You can also type “remind me to call home in 20 minutes” in a chat.",
            font=f.ui(12), text_color=SUBTLE, anchor="w",
        ).pack(fill="x", padx=4, pady=(6, 4))
        reminder_list = self._scroll_list(reminders_tab)
        reminder_list.pack(fill="both", expand=True, pady=(4, 0))

        status_style = {
            "Pending": ("#3a3220", WARNING),
            "Notified": (ACCENT_SOFT, ACCENT),
            "Done": ("#1d3326", SUCCESS),
        }

        def refresh_reminders():
            for child in reminder_list.winfo_children():
                child.destroy()
            rows = list_reminders_db(include_completed=True)
            if not rows:
                self._empty_state(reminder_list, Icon.CLOCK, "No reminders",
                                  "Add one above. Nova shows a pop-up when it's due.")
                return
            for row in rows:
                reminder_id, title, due_at, completed, notified = row[0], row[1], row[2], int(row[3]), row[5]
                status = "Done" if completed else ("Notified" if notified else "Pending")
                card = ctk.CTkFrame(reminder_list, fg_color=SURFACE, corner_radius=12)
                card.pack(fill="x", pady=3, padx=(0, 8))
                ctk.CTkLabel(card, text=Icon.CHECK if completed else Icon.CLOCK, font=f.icon(16),
                             text_color=SUCCESS if completed else MUTED, width=24).pack(side="left", padx=(16, 10), pady=12)
                text_box = ctk.CTkFrame(card, fg_color="transparent")
                text_box.pack(side="left", fill="x", expand=True, pady=8)
                ctk.CTkLabel(text_box, text=title, font=f.ui(14, semibold=True),
                             text_color=SUBTLE if completed else TEXT, anchor="w").pack(fill="x")
                ctk.CTkLabel(text_box, text=f"Due {due_at}", font=f.ui(12), text_color=MUTED, anchor="w").pack(fill="x")
                remove = icon_button(card, f, Icon.DELETE, lambda rid=reminder_id: delete_reminder(rid),
                                     size=30, icon_size=12, hover=DANGER_SOFT, text_color=SUBTLE)
                remove.pack(side="right", padx=(2, 12))
                Tooltip(remove, "Delete reminder", f)
                if not completed:
                    done = icon_button(card, f, Icon.CHECK, lambda rid=reminder_id: complete_reminder(rid),
                                       size=30, icon_size=13, text_color=SUBTLE)
                    done.pack(side="right", padx=2)
                    Tooltip(done, "Mark as done", f)
                chip_fg, chip_text = status_style[status]
                self._chip(card, status, chip_fg, chip_text).pack(side="right", padx=8)

        def add_reminder(_event=None):
            title = reminder_title.get().strip()
            due_text = reminder_due.get().strip()
            if not title:
                reminder_title.focus_set()
                return
            try:
                due_dt = datetime.strptime(due_text, "%Y-%m-%d %H:%M")
            except ValueError:
                messagebox.showwarning("Reminders", "Use YYYY-MM-DD HH:MM, for example 2026-11-07 09:30.", parent=win)
                return
            if due_dt <= datetime.now():
                messagebox.showwarning("Reminders", "Choose a future date and time.", parent=win)
                return
            try:
                save_reminder_db(title, due_dt.strftime("%Y-%m-%d %H:%M"))
                reminder_title.delete(0, "end")
                refresh_reminders()
                self.set_status("Reminder saved")
            except Exception as exc:
                messagebox.showerror("Reminders", str(exc), parent=win)

        def complete_reminder(reminder_id):
            try:
                complete_reminder_db(reminder_id)
                refresh_reminders()
                self.set_status("Reminder marked done")
            except Exception as exc:
                messagebox.showerror("Reminders", str(exc), parent=win)

        def delete_reminder(reminder_id):
            if not messagebox.askyesno("Delete reminder", "Permanently delete this reminder?", parent=win):
                return
            try:
                delete_reminder_db(reminder_id)
                refresh_reminders()
                self.set_status("Reminder deleted")
            except Exception as exc:
                messagebox.showerror("Reminders", str(exc), parent=win)

        self._button(form, "Add", add_reminder, width=80).pack(side="right", padx=(6, 12), pady=12)
        reminder_title.bind("<Return>", add_reminder)
        reminder_due.bind("<Return>", add_reminder)
        refresh_notes()
        refresh_reminders()

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

        menu = pystray.Menu(
            pystray.MenuItem("Open Nova", open_window, default=True),
            pystray.MenuItem("Hide to tray", hide_window),
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
        f = self.fonts
        win = self._dialog("Settings", "580x500", resizable=False)
        body = ctk.CTkFrame(win, fg_color=BG)
        body.pack(fill="both", expand=True, padx=26, pady=22)
        self._dialog_header(body, "Settings", "Choose how Nova runs in the background.")

        startup_var = tk.BooleanVar(value=startup_enabled())
        tray_var = tk.BooleanVar(value=bool(self.settings.get("minimize_to_tray", True)))
        tray_available = bool(self.tray_dependencies)

        card = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14)
        card.pack(fill="x")

        def setting_row(parent, title, description, variable, enabled=True, last=False):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=(16, 16 if last else 12))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(text, text=title, font=f.ui(14, semibold=True), text_color=TEXT, anchor="w").pack(fill="x")
            ctk.CTkLabel(text, text=description, font=f.ui(12), text_color=MUTED, anchor="w",
                         justify="left", wraplength=400).pack(fill="x", pady=(2, 0))
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
            switch.pack(side="right")
            return switch

        setting_row(card, "Start with Windows",
                    "Open Nova automatically when you sign in. Applies to your Windows account only.",
                    startup_var)
        ctk.CTkFrame(card, height=1, fg_color=BORDER).pack(fill="x", padx=18)
        tray_description = (
            "Closing the window hides Nova in the system tray, so reminders keep working."
            if tray_available else
            "Unavailable: run “pip install pystray pillow” in Nova's virtual environment, then restart Nova."
        )
        setting_row(card, "Keep running in the tray", tray_description, tray_var,
                    enabled=tray_available, last=True)

        about = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=14)
        about.pack(fill="x", pady=(14, 0))
        for label, value in (
            ("Model", MODEL),
            ("Speech", "whisper.cpp · Vosk wake word"),
            ("Data", "nova_memory.db on this PC"),
        ):
            line = ctk.CTkFrame(about, fg_color="transparent")
            line.pack(fill="x", padx=18, pady=(8 if label == "Model" else 2, 8 if label == "Data" else 2))
            ctk.CTkLabel(line, text=label, font=f.ui(13), text_color=MUTED, width=70, anchor="w").pack(side="left")
            ctk.CTkLabel(line, text=value, font=f.ui(13), text_color=TEXT, anchor="w").pack(side="left")

        def apply_settings():
            try:
                if startup_var.get() != startup_enabled():
                    set_startup_enabled(startup_var.get())
                self.settings["minimize_to_tray"] = bool(tray_var.get())
                if not save_nova_settings(self.settings):
                    raise RuntimeError(f"Could not save settings to {SETTINGS_PATH}")
                if tray_var.get() and tray_available and self.tray_icon is None:
                    self.start_tray_icon()
                self.set_status("Settings saved")
                win.destroy()
            except Exception as exc:
                messagebox.showerror("Settings", str(exc), parent=win)

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", fill="x")
        self._button(buttons, "Save", apply_settings, width=100).pack(side="right")
        self._button(buttons, "Cancel", win.destroy, kind="secondary", width=100).pack(side="right", padx=8)

    # -------------------- exit --------------------
    def exit_application(self):
        if self._exiting:
            return
        if self.busy and not messagebox.askyesno("Exit Nova", "A task is still running. Exit anyway?", parent=self.root):
            return
        self._exiting = True
        self.wake_active = False
        self.wake_stop_event.set()
        self.wake_resume_event.set()
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def close(self):
        if self.settings.get("minimize_to_tray", True) and self.tray_dependencies and self.tray_icon:
            self.hide_to_tray()
            return
        self.exit_application()


def main():
    ctk.set_appearance_mode("dark")
    initialize_database()
    initialize_productivity_tables()
    root = ctk.CTk()
    NovaGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

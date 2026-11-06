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
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

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

APP_TITLE = "Nova AI"
MODEL = getattr(backend, "MODEL", "gemma3:4b")
BG = "#10141c"
PANEL = "#171d28"
PANEL_2 = "#202838"
TEXT = "#edf2f7"
MUTED = "#9aa8ba"
ACCENT = "#75a7ff"
USER_BUBBLE = "#243b5a"
BOT_BUBBLE = "#1c2634"
BORDER = "#303b4d"



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


class NovaGUI:
    def __init__(self, root):
        self.root = root
        self.settings = load_nova_settings()
        self.tray_dependencies = load_tray_dependencies()
        self.tray_icon = None
        self.tray_thread = None
        self._exiting = False
        self.root.title(APP_TITLE)
        self.root.geometry("1120x760")
        self.root.minsize(850, 600)
        self.root.configure(bg=BG)
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
        self._configure_styles()
        self._build_ui()
        self.refresh_sessions(select_current=True)
        self.load_chat()
        self.refresh_memories()
        initialize_productivity_tables()
        self.set_status("Ready • Local mode")
        self.root.after(100, self.poll_events)
        self.root.after(1000, self.poll_due_reminders)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.tray_dependencies:
            self.start_tray_icon()

    def _configure_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 18))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        style.configure("Accent.TButton", background=ACCENT, foreground="#0b1220", font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#9bbdff"), ("disabled", "#536782")])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=29, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL_2, foreground=TEXT, font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#304b70")], foreground=[("selected", TEXT)])

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 14, 18, 12))
        header.pack(fill="x")
        left_head = ttk.Frame(header)
        left_head.pack(side="left", fill="x", expand=True)
        ttk.Label(left_head, text="Nova AI", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left_head, text=f"Private desktop assistant  •  Model: {MODEL}  •  Runs locally", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        self.status_var = tk.StringVar(value="Starting…")
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.pack(side="right", anchor="e")

        body = ttk.Frame(self.root, padding=(14, 0, 14, 12))
        body.pack(fill="both", expand=True)

        sidebar = ttk.Frame(body, style="Panel.TFrame", padding=10, width=240)
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="CONVERSATIONS", style="Panel.TLabel", font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(2, 8))
        ttk.Button(sidebar, text="+ New chat", command=self.new_chat).pack(fill="x", pady=(0, 8))
        self.session_tree = ttk.Treeview(sidebar, columns=("date",), show="tree", selectmode="browse", height=13)
        self.session_tree.pack(fill="both", expand=True)
        self.session_tree.bind("<<TreeviewSelect>>", self.on_session_select)
        ttk.Separator(sidebar).pack(fill="x", pady=12)
        ttk.Label(sidebar, text="LOCAL MEMORY", style="Panel.TLabel", font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(0, 6))
        ttk.Label(sidebar, text="Only memories you explicitly save are stored.", style="Muted.TLabel", wraplength=205).pack(anchor="w")
        ttk.Button(sidebar, text="Manage memories", command=self.open_memories).pack(fill="x", pady=(8, 0))
        ttk.Button(sidebar, text="Notes & reminders", command=self.open_productivity).pack(fill="x", pady=(8, 0))
        ttk.Button(sidebar, text="Startup & tray settings", command=self.open_startup_settings).pack(fill="x", pady=(8, 0))

        main = ttk.Frame(body, style="Panel.TFrame", padding=12)
        main.pack(side="left", fill="both", expand=True)
        chat_head = ttk.Frame(main, style="Panel.TFrame")
        chat_head.pack(fill="x", pady=(0, 8))
        self.chat_title = ttk.Label(chat_head, text="Conversation", style="Panel.TLabel", font=("Segoe UI Semibold", 12))
        self.chat_title.pack(side="left")
        ttk.Label(chat_head, text="Chats and memories stay on this PC", style="Muted.TLabel").pack(side="right")

        chat_container = ttk.Frame(main, style="Panel.TFrame")
        chat_container.pack(fill="both", expand=True)
        self.chat_text = tk.Text(
            chat_container, wrap="word", state="disabled", bg=PANEL, fg=TEXT,
            insertbackground=TEXT, selectbackground="#304b70", relief="flat",
            borderwidth=0, padx=12, pady=12, font=("Segoe UI", 10),
            spacing1=3, spacing3=9,
        )
        scrollbar = ttk.Scrollbar(chat_container, orient="vertical", command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scrollbar.set)
        self.chat_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.chat_text.tag_configure("user_label", foreground="#a9c9ff", font=("Segoe UI Semibold", 10))
        self.chat_text.tag_configure("assistant_label", foreground="#9de2c3", font=("Segoe UI Semibold", 10))
        self.chat_text.tag_configure("body", foreground=TEXT, lmargin1=4, lmargin2=4, spacing3=10)
        self.chat_text.tag_configure("system", foreground=MUTED, font=("Segoe UI", 9), lmargin1=4, lmargin2=4)

        composer = ttk.Frame(main, style="Panel.TFrame")
        composer.pack(fill="x", pady=(10, 0))
        self.input_text = tk.Text(
            composer, height=3, wrap="word", bg=PANEL_2, fg=TEXT,
            insertbackground=TEXT, relief="flat", borderwidth=0,
            padx=10, pady=9, font=("Segoe UI", 10),
        )
        self.input_text.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.input_text.bind("<Control-Return>", self._send_shortcut)
        self.input_text.bind("<Return>", self._enter_behavior)
        controls = ttk.Frame(composer, style="Panel.TFrame")
        controls.pack(side="right", fill="y")
        self.voice_button = ttk.Button(controls, text="🎙 Voice", command=self.start_voice)
        self.voice_button.pack(fill="x", pady=(0, 6))
        self.wake_button = ttk.Button(controls, text="Hey Nova: OFF", command=self.toggle_wake_word)
        self.wake_button.pack(fill="x", pady=(0, 6))
        self.send_button = ttk.Button(controls, text="Send", style="Accent.TButton", command=self.send_text)
        self.send_button.pack(fill="x")
        ttk.Label(main, text="Enter to send • Shift+Enter for a new line • Ctrl+Enter to send", style="Muted.TLabel").pack(anchor="w", pady=(7, 0))

    def set_status(self, value):
        self.status_var.set(value)

    def append_message(self, role, content):
        self.chat_text.configure(state="normal")
        if role == "user":
            self.chat_text.insert("end", "You\n", "user_label")
        elif role == "assistant":
            self.chat_text.insert("end", "Nova\n", "assistant_label")
        else:
            self.chat_text.insert("end", "Info\n", "system")
        self.chat_text.insert("end", content.rstrip() + "\n\n", "body" if role in ("user", "assistant") else "system")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def clear_chat_view(self):
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        self.chat_text.configure(state="disabled")

    def load_chat(self):
        self.clear_chat_view()
        items = fetch_messages(self.session_id)
        if not items:
            self.append_message("system", "Hi, I'm Nova. Ask a question or press Voice to speak. Everything is processed locally.")
        else:
            for role, content in items:
                self.append_message(role, content)
        self.chat_title.configure(text=f"Conversation #{self.session_id}")

    def refresh_sessions(self, select_current=False):
        self.sessions = fetch_sessions()
        self.session_tree.delete(*self.session_tree.get_children())
        for sid, label, created in self.sessions:
            display = label if label and not label.startswith("Chat ") else f"Chat {sid}"
            if created:
                display = f"{display}  ·  {created[:10]}"
            self.session_tree.insert("", "end", iid=str(sid), text=display)
        if select_current and self.session_id is not None and self.session_tree.exists(str(self.session_id)):
            self.session_tree.selection_set(str(self.session_id))
            self.session_tree.see(str(self.session_id))

    def on_session_select(self, _event=None):
        selected = self.session_tree.selection()
        if not selected:
            return
        try:
            new_id = int(selected[0])
        except ValueError:
            new_id = selected[0]
        if new_id == self.session_id:
            return
        if self.busy:
            # Keep the sidebar in sync with the chat being shown: the running
            # reply belongs to the current conversation. Re-selecting fires
            # this handler again, where new_id == session_id makes it a no-op.
            if self.session_tree.exists(str(self.session_id)):
                self.session_tree.selection_set(str(self.session_id))
            else:
                self.session_tree.selection_remove(selected)
            self.set_status("Wait for Nova to finish before switching conversations")
            return
        self.session_id = new_id
        self.load_chat()

    def new_chat(self):
        if self.busy:
            self.set_status("Wait for Nova to finish before starting a new chat")
            return
        self.session_id = new_session_id()
        if self.session_id is None:
            messagebox.showerror("Nova AI", "Could not create a new conversation.")
            return
        self.refresh_sessions(select_current=True)
        self.load_chat()
        self.set_status("New conversation ready")

    def _enter_behavior(self, event):
        if event.state & 0x1:  # Shift+Enter inserts newline
            return None
        self.send_text()
        return "break"

    def _send_shortcut(self, _event):
        self.send_text()
        return "break"

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
        self.set_status("Action completed • Local mode")
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
        self.append_message("user", text)
        self.handle_user_message(text, speak=False)

    def toggle_wake_word(self):
        if self.wake_active:
            self.wake_active = False
            self.wake_stop_event.set()
            self.wake_resume_event.set()
            self.wake_button.configure(text="Hey Nova: OFF")
            self.set_controls_enabled(not self.busy)
            self.set_status("Stopping wake-word listener…")
            return
        if self.busy:
            messagebox.showinfo("Hey Nova", "Wait for the current task to finish before enabling wake-word mode.")
            return
        self.wake_active = True
        self.wake_stop_event.clear()
        self.wake_resume_event.clear()
        self.wake_button.configure(text="Hey Nova: ON")
        self.set_controls_enabled(True)  # disables Voice while wake mode is on
        self.set_status("Starting offline wake-word listener…")
        self.events.put(("system", "Wake-word mode is on. Say “Hey Nova”, then speak your command after detection."))
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
        self.events.put(("system", "Voice mode started. Speak clearly after the microphone opens."))
        threading.Thread(target=self._voice_worker, daemon=True).start()

    def _voice_worker(self):
        try:
            transcript = record_and_transcribe(lambda s: self.events.put(("status", s)))
            self.events.put(("transcript_ready", transcript))
        except Exception as exc:
            self.events.put(("voice_error", str(exc)))

    def begin_generation(self, user_text, speak=False):
        if self.busy:
            return
        self.busy = True
        self.voice_response = speak
        self.set_controls_enabled(False)
        self.set_status("Thinking locally…")
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
            messages =[{"role": "system", "content": system_prompt(user_text)}]
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
        self.send_button.configure(state=state)
        # Voice and Hey Nova share the microphone; never allow both at once.
        self.voice_button.configure(
            state="normal" if enabled and not self.wake_active else "disabled"
        )
        if enabled:
            self.input_text.configure(state="normal")
        else:
            self.input_text.configure(state="disabled")

    def poll_events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    self.set_status(payload)
                elif event == "system":
                    self.append_message("system", payload)
                elif event == "assistant_start":
                    self.chat_text.configure(state="normal")
                    self.chat_text.insert("end", "Nova\n", "assistant_label")
                    self.chat_text.configure(state="disabled")
                    self._stream_start = True
                    self.chat_text.see("end")
                elif event == "token":
                    self.chat_text.configure(state="normal")
                    self.chat_text.insert("end", payload, "body")
                    self.chat_text.configure(state="disabled")
                    self.chat_text.see("end")
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
                    if messagebox.askyesno("Confirm voice input", f"Send this transcript to Nova?\n\n{payload}"):
                        self.append_message("user", payload)
                        self.handle_user_message(payload, speak=True)
                    else:
                        self.set_status("Command cancelled • wake word remains active")
                        self.wake_resume_event.set()
                elif event == "wake_voice_error":
                    self.set_status("Wake-word voice capture failed")
                    messagebox.showerror("Wake-word voice input", payload)
                    self.wake_resume_event.set()
                elif event == "wake_setup_error":
                    self.wake_active = False
                    self.wake_stop_event.set()
                    self.wake_button.configure(text="Hey Nova: OFF")
                    self.set_controls_enabled(not self.busy)
                    self.set_status("Wake-word setup failed")
                    messagebox.showerror("Hey Nova setup", payload)
                elif event == "transcript_ready":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Transcript ready")
                    if messagebox.askyesno("Confirm voice input", f"Send this transcript to Nova?\n\n{payload}"):
                        self.append_message("user", payload)
                        self.handle_user_message(payload, speak=True)
                    else:
                        self.set_status("Voice input cancelled")
                elif event == "voice_error":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Ready • Voice input failed")
                    messagebox.showerror("Voice input", payload)
                elif event == "generation_done":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Ready • Local mode")
                    self.refresh_sessions(select_current=True)
                    if self.wake_active:
                        self.wake_resume_event.set()
                elif event == "generation_error":
                    self.busy = False
                    self.set_controls_enabled(True)
                    self.set_status("Error • Check local services")
                    self.append_message("system", payload)
                    messagebox.showerror("Nova AI", payload)
                    if self.wake_active:
                        self.wake_resume_event.set()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def refresh_memories(self):
        try:
            self.memory_rows = get_memories()
        except Exception:
            self.memory_rows = []

    def open_memories(self):
        win = tk.Toplevel(self.root)
        win.title("Nova — Long-term memory")
        win.geometry("680x480")
        win.configure(bg=BG)
        win.transient(self.root)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Long-term memory", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Only facts you explicitly add here are saved for future chats.", style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
        tree = ttk.Treeview(frame, columns=("category", "content", "updated"), show="headings", selectmode="browse")
        tree.heading("category", text="Category")
        tree.heading("content", text="Memory")
        tree.heading("updated", text="Updated")
        tree.column("category", width=105, stretch=False)
        tree.column("content", width=390)
        tree.column("updated", width=105, stretch=False)
        tree.pack(fill="both", expand=True)

        def refresh():
            tree.delete(*tree.get_children())
            for row in get_memories():
                try:
                    tree.insert("", "end", iid=str(row[0]), values=(row[1], row[2], format_timestamp(row[3])))
                except Exception:
                    pass

        form = ttk.Frame(frame)
        form.pack(fill="x", pady=(10, 0))
        category_var = tk.StringVar(value="general")
        content_var = tk.StringVar()
        ttk.Label(form, text="Category").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(form, textvariable=category_var, width=16).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(form, text="Memory").grid(row=0, column=2, sticky="w", padx=(0, 6))
        entry = ttk.Entry(form, textvariable=content_var)
        entry.grid(row=0, column=3, sticky="ew")
        form.columnconfigure(3, weight=1)

        def add():
            content = content_var.get().strip()
            if not content:
                messagebox.showwarning("Memory", "Enter a memory to save.", parent=win)
                return
            try:
                created = add_memory_db(category_var.get(), content)
                content_var.set("")
                refresh()
                self.set_status(
                    "Memory saved locally" if created
                    else "Memory already saved • moved to top"
                )
            except ValueError as exc:
                messagebox.showwarning("Memory", str(exc), parent=win)
            except Exception as exc:
                messagebox.showerror("Memory", str(exc), parent=win)

        def delete():
            selected = tree.selection()
            if not selected:
                return
            if not messagebox.askyesno("Delete memory", "Delete the selected memory?", parent=win):
                return
            try:
                delete_memory_db(int(selected[0]))
                refresh()
                self.set_status("Memory deleted")
            except Exception as exc:
                messagebox.showerror("Memory", str(exc), parent=win)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Save memory", command=add).pack(side="left")
        ttk.Button(buttons, text="Delete selected", command=delete).pack(side="left", padx=8)
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
        refresh()
        entry.focus_set()


    def poll_due_reminders(self):
        """Check due reminders on Tk's main thread; persist notification state first."""
        try:
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
            due = claim_due_reminders_db(now_text)
            for reminder in due:
                reminder_id, title, due_at = reminder[0], reminder[1], reminder[2]
                self.set_status(f"Reminder due: {title}")
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

    def open_productivity(self):
        """Open local Notes and Reminders manager."""
        win = tk.Toplevel(self.root)
        win.title("Nova — Notes & reminders")
        win.geometry("860x600")
        win.minsize(720, 480)
        win.configure(bg=BG)
        win.transient(self.root)

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Notes & reminders", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Stored locally in Nova's existing SQLite database. Reminder times use this PC's local time.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 10))

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)

        # Notes tab
        notes_tab = ttk.Frame(tabs, padding=10)
        tabs.add(notes_tab, text="Notes")
        note_tree = ttk.Treeview(
            notes_tab, columns=("title", "updated"), show="headings", selectmode="browse", height=8
        )
        note_tree.heading("title", text="Title")
        note_tree.heading("updated", text="Last updated")
        note_tree.column("title", width=510)
        note_tree.column("updated", width=150, stretch=False)
        note_tree.pack(fill="both", expand=True)

        note_form = ttk.Frame(notes_tab)
        note_form.pack(fill="both", expand=False, pady=(10, 0))
        ttk.Label(note_form, text="Title").grid(row=0, column=0, sticky="w", padx=(0, 8))
        note_title = tk.StringVar()
        ttk.Entry(note_form, textvariable=note_title).grid(row=0, column=1, sticky="ew")
        ttk.Label(note_form, text="Content").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=(8, 0))
        note_content = tk.Text(
            note_form, height=5, wrap="word", bg=PANEL_2, fg=TEXT,
            insertbackground=TEXT, relief="flat", borderwidth=0, padx=8, pady=7,
        )
        note_content.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
        note_form.columnconfigure(1, weight=1)

        selected_note_id = {"value": None}

        def refresh_notes(select_id=None):
            note_tree.delete(*note_tree.get_children())
            for row in list_notes_db():
                note_tree.insert("", "end", iid=str(row[0]), values=(row[1], format_timestamp(row[4] or row[3])))
            if select_id is not None and note_tree.exists(str(select_id)):
                note_tree.selection_set(str(select_id))

        def load_selected_note(_event=None):
            selection = note_tree.selection()
            if not selection:
                return
            try:
                note_id = int(selection[0])
                row = next((r for r in list_notes_db() if int(r[0]) == note_id), None)
                if row is None:
                    return
                selected_note_id["value"] = note_id
                note_title.set(str(row[1]))
                note_content.delete("1.0", "end")
                note_content.insert("1.0", str(row[2] or ""))
            except Exception as exc:
                messagebox.showerror("Notes", str(exc), parent=win)

        note_tree.bind("<<TreeviewSelect>>", load_selected_note)

        def clear_note_form():
            selected_note_id["value"] = None
            note_title.set("")
            note_content.delete("1.0", "end")
            note_tree.selection_remove(note_tree.selection())

        def save_note():
            title = note_title.get().strip()
            content = note_content.get("1.0", "end").strip()
            if not title:
                messagebox.showwarning("Notes", "Enter a note title.", parent=win)
                return
            try:
                note_id = save_note_db(title, content, selected_note_id["value"])
                selected_note_id["value"] = note_id
                refresh_notes(note_id)
                self.set_status("Note saved locally")
            except Exception as exc:
                messagebox.showerror("Notes", str(exc), parent=win)

        def delete_note():
            note_id = selected_note_id["value"]
            if note_id is None:
                selection = note_tree.selection()
                if selection:
                    note_id = int(selection[0])
            if note_id is None:
                messagebox.showinfo("Notes", "Select a note to delete.", parent=win)
                return
            if not messagebox.askyesno("Delete note", "Permanently delete this note?", parent=win):
                return
            try:
                delete_note_db(note_id)
                clear_note_form()
                refresh_notes()
                self.set_status("Note deleted")
            except Exception as exc:
                messagebox.showerror("Notes", str(exc), parent=win)

        note_buttons = ttk.Frame(notes_tab)
        note_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(note_buttons, text="New note", command=clear_note_form).pack(side="left")
        ttk.Button(note_buttons, text="Save note", command=save_note).pack(side="left", padx=6)
        ttk.Button(note_buttons, text="Delete selected", command=delete_note).pack(side="left")

        # Reminders tab
        reminders_tab = ttk.Frame(tabs, padding=10)
        tabs.add(reminders_tab, text="Reminders")
        reminder_tree = ttk.Treeview(
            reminders_tab,
            columns=("title", "due", "status"),
            show="headings",
            selectmode="browse",
            height=11,
        )
        reminder_tree.heading("title", text="Reminder")
        reminder_tree.heading("due", text="Due (local time)")
        reminder_tree.heading("status", text="Status")
        reminder_tree.column("title", width=370)
        reminder_tree.column("due", width=160, stretch=False)
        reminder_tree.column("status", width=100, stretch=False)
        reminder_tree.pack(fill="both", expand=True)

        reminder_form = ttk.Frame(reminders_tab)
        reminder_form.pack(fill="x", pady=(12, 0))
        reminder_title = tk.StringVar()
        default_due = (datetime.now() + timedelta(hours=1)).replace(second=0, microsecond=0)
        reminder_due = tk.StringVar(value=default_due.strftime("%Y-%m-%d %H:%M"))
        ttk.Label(reminder_form, text="Reminder").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(reminder_form, textvariable=reminder_title).grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(reminder_form, text="Date & time").grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Entry(reminder_form, textvariable=reminder_due, width=19).grid(row=0, column=3, sticky="ew")
        ttk.Label(
            reminders_tab,
            text="Enter date/time as YYYY-MM-DD HH:MM (24-hour clock). Nova must be running to show reminders.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(5, 0))
        reminder_form.columnconfigure(1, weight=1)

        def refresh_reminders():
            reminder_tree.delete(*reminder_tree.get_children())
            for row in list_reminders_db(include_completed=True):
                status = "Completed" if int(row[3]) else ("Notified" if row[5] else "Pending")
                reminder_tree.insert("", "end", iid=str(row[0]), values=(row[1], row[2], status))

        def add_reminder():
            title = reminder_title.get().strip()
            due_text = reminder_due.get().strip()
            if not title:
                messagebox.showwarning("Reminders", "Enter a reminder.", parent=win)
                return
            try:
                due_dt = datetime.strptime(due_text, "%Y-%m-%d %H:%M")
            except ValueError:
                messagebox.showwarning(
                    "Reminders", "Use YYYY-MM-DD HH:MM, for example 2026-11-02 09:30.", parent=win
                )
                return
            if due_dt <= datetime.now():
                messagebox.showwarning("Reminders", "Choose a future date and time.", parent=win)
                return
            try:
                save_reminder_db(title, due_dt.strftime("%Y-%m-%d %H:%M"))
                reminder_title.set("")
                refresh_reminders()
                self.set_status("Reminder saved locally")
            except Exception as exc:
                messagebox.showerror("Reminders", str(exc), parent=win)

        def complete_reminder():
            selection = reminder_tree.selection()
            if not selection:
                messagebox.showinfo("Reminders", "Select a reminder first.", parent=win)
                return
            try:
                complete_reminder_db(int(selection[0]))
                refresh_reminders()
                self.set_status("Reminder marked complete")
            except Exception as exc:
                messagebox.showerror("Reminders", str(exc), parent=win)

        def delete_reminder():
            selection = reminder_tree.selection()
            if not selection:
                messagebox.showinfo("Reminders", "Select a reminder first.", parent=win)
                return
            if not messagebox.askyesno("Delete reminder", "Permanently delete this reminder?", parent=win):
                return
            try:
                delete_reminder_db(int(selection[0]))
                refresh_reminders()
                self.set_status("Reminder deleted")
            except Exception as exc:
                messagebox.showerror("Reminders", str(exc), parent=win)

        reminder_buttons = ttk.Frame(reminders_tab)
        reminder_buttons.pack(fill="x", pady=(9, 0))
        ttk.Button(reminder_buttons, text="Add reminder", command=add_reminder).pack(side="left")
        ttk.Button(reminder_buttons, text="Mark complete", command=complete_reminder).pack(side="left", padx=6)
        ttk.Button(reminder_buttons, text="Delete selected", command=delete_reminder).pack(side="left")
        refresh_notes()
        refresh_reminders()

    def start_tray_icon(self):
        if not self.tray_dependencies or self.tray_icon is not None:
            return
        pystray, Image, ImageDraw = self.tray_dependencies
        image = Image.new("RGBA", (64, 64), (16, 20, 28, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=15, fill=(117, 167, 255, 255))
        draw.ellipse((17, 17, 47, 47), fill=(16, 20, 28, 255))
        draw.ellipse((24, 24, 40, 40), fill=(157, 226, 195, 255))

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
        self.tray_icon = pystray.Icon("NovaAI", image, "Nova AI", menu)
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

    def open_startup_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Nova — Startup & tray settings")
        win.geometry("590x400")
        win.resizable(False, False)
        win.transient(self.root)
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Startup & background operation", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Configure Nova to start with your Windows account and remain available from the system tray.",
            style="Muted.TLabel",
            wraplength=535,
        ).pack(anchor="w", pady=(5, 14))

        startup_var = tk.BooleanVar(value=startup_enabled())
        tray_var = tk.BooleanVar(value=bool(self.settings.get("minimize_to_tray", True)))
        ttk.Checkbutton(
            frame,
            text="Start Nova automatically when I sign in to Windows",
            variable=startup_var,
        ).pack(anchor="w", pady=(0, 10))

        tray_available = bool(self.tray_dependencies)
        tray_check = ttk.Checkbutton(
            frame,
            text="Keep Nova running in the system tray when I close its window",
            variable=tray_var,
        )
        tray_check.pack(anchor="w", pady=(0, 5))
        if not tray_available:
            tray_check.configure(state="disabled")
            ttk.Label(
                frame,
                text="System tray support is not installed. In your activated .venv, run:\npip install pystray pillow\nThen restart Nova.",
                style="Muted.TLabel",
                wraplength=530,
            ).pack(anchor="w", pady=(0, 10))

        ttk.Label(
            frame,
            text="When hidden, Nova remains running and can continue checking reminders. Use the tray menu to reopen or exit. Startup applies to the current Windows user only.",
            style="Muted.TLabel",
            wraplength=530,
        ).pack(anchor="w", pady=(10, 14))

        def apply_settings():
            try:
                if startup_var.get() != startup_enabled():
                    set_startup_enabled(startup_var.get())
                self.settings["minimize_to_tray"] = bool(tray_var.get())
                if not save_nova_settings(self.settings):
                    raise RuntimeError(f"Could not save settings to {SETTINGS_PATH}")
                if tray_var.get() and tray_available and self.tray_icon is None:
                    self.start_tray_icon()
                messagebox.showinfo("Nova settings", "Settings saved.", parent=win)
                win.destroy()
            except Exception as exc:
                messagebox.showerror("Nova settings", str(exc), parent=win)

        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="Save settings", command=apply_settings).pack(side="left")
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="left", padx=8)

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
    initialize_database()
    initialize_productivity_tables()
    root = tk.Tk()
    app = NovaGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

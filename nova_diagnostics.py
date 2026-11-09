"""
Diagnostics for Nova: a status report of the app, Ollama, the selected model,
the database, backups, logging and optional components. No GUI code.

Nothing secret is included: no environment variables, no chat or document
content, and credentials in the Ollama address are masked.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from nova_system import NOVA_VERSION, redact

OK, WARNING, ERROR, INFO = "ok", "warning", "error", "info"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}

# (import name, distribution name, what it's for, required)
COMPONENTS = (
    ("customtkinter", "customtkinter", "Desktop interface", True),
    ("ollama", "ollama", "Talking to the local model", True),
    ("numpy", "numpy", "Voice input", False),
    ("scipy", "scipy", "Voice input (saving recordings)", False),
    ("sounddevice", "sounddevice", "Microphone", False),
    ("pyttsx3", "pyttsx3", "Spoken replies", False),
    ("vosk", "vosk", "“Hey Nova” wake word", False),
    ("pystray", "pystray", "System tray icon", False),
    ("PIL", "Pillow", "Tray and window icons", False),
    ("pypdf", "pypdf", "Reading PDFs", False),
)


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str


def ollama_host():
    """The Ollama address in use, with any credentials masked."""
    raw = os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or ""
    shown = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    return shown, host.lower() in LOCAL_HOSTS


def describe_ollama_error(exc, model, host=None):
    """A clear, user-facing explanation of an Ollama failure."""
    host = host or ollama_host()[0]
    status = getattr(exc, "status_code", None)
    text = str(exc)
    if type(exc).__name__ == "OllamaUnavailable":   # already a user-facing message
        return f"{text} (address: {host})"
    if status == 404 or "not found" in text.lower() and "model" in text.lower():
        return (f"The model “{model}” isn't installed in Ollama. Run “ollama pull {model}” in a terminal, "
                "or choose an installed model in Settings › Model.")
    name = type(exc).__name__
    if isinstance(exc, (ConnectionError, TimeoutError)) or name in ("ConnectError", "ConnectTimeout", "ReadTimeout",
                                                                    "ConnectionRefusedError"):
        return f"Nova couldn't reach Ollama at {host}. Start the Ollama app (or check it's installed), then try again."
    if status and status >= 500:
        return f"Ollama reported an internal error ({status}). It may be out of memory; try a smaller model. Details: {text}"
    return f"Ollama returned an error: {text}"


def python_is_emulated():
    """True when x64 Python runs under emulation on an ARM64 Windows PC."""
    if platform.machine().upper() not in ("AMD64", "X86_64"):
        return False
    identifier = (os.environ.get("PROCESSOR_IDENTIFIER", "") + " " + platform.processor()).upper()
    return "ARM" in identifier or "QUALCOMM" in identifier


def component_status(import_name, distribution):
    """(installed, version or None) without importing the package."""
    try:
        found = importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        return False, None
    try:
        return True, importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return True, None


def _size(num):
    for unit in ("bytes", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "bytes" else f"{num:.1f} {unit}"
        num /= 1024


def run_checks(db_path, model, backup_folder, log_path, settings_warnings=(), list_models=None,
               find_whisper=None, find_vosk_model=None, count_input_devices=None, performance=None, online=None):
    """Collect every check. Never raises: failures become ERROR/WARNING rows."""
    checks = []
    add = lambda area, name, status, detail: checks.append(Check(area, name, status, redact(detail, 500)))

    # App
    bits = struct.calcsize("P") * 8
    add("App", "Nova version", INFO, NOVA_VERSION)
    add("App", "Python", INFO, f"{platform.python_version()} ({bits}-bit, {platform.machine() or 'unknown CPU'})")
    add("App", "Windows", INFO, platform.platform())
    add("App", "SQLite", INFO, sqlite3.sqlite_version)

    # Ollama and the model
    host, local = ollama_host()
    add("Ollama", "Address", OK if local else WARNING,
        host + ("" if local else " — not this PC: chats would be sent to another computer (OLLAMA_HOST)"))
    if list_models is None:
        add("Ollama", "Connection", INFO, "Not checked")
    else:
        try:
            models = list_models()
            add("Ollama", "Connection", OK, f"Running · {len(models)} chat model{'s' if len(models) != 1 else ''} installed")
            installed = model in models or f"{model}:latest" in models
            add("Ollama", "Selected model", OK if installed else ERROR,
                f"{model} is installed" if installed else
                f"{model} isn't installed. Run “ollama pull {model}” or choose another in Settings › Model.")
        except Exception as exc:
            add("Ollama", "Connection", ERROR, describe_ollama_error(exc, model, host))
            add("Ollama", "Selected model", WARNING, f"{model} (can't check while Ollama is unreachable)")

    # Database
    import nova_backup
    db_path = Path(db_path)
    if not db_path.exists():
        add("Database", "File", WARNING, "Not created yet")
    else:
        healthy, message, version = nova_backup.check_database(db_path)
        size = db_path.stat().st_size + sum(Path(str(db_path) + s).stat().st_size
                                            for s in ("-wal", "-shm") if Path(str(db_path) + s).exists())
        add("Database", "File", OK, f"{db_path.name} · {_size(size)}")
        add("Database", "Integrity", OK if healthy and message == "OK" else (WARNING if healthy else ERROR), message)
        add("Database", "Schema version", OK if version <= nova_backup.SCHEMA_VERSION else WARNING,
            f"{version} (this Nova: {nova_backup.SCHEMA_VERSION})")
        if healthy:
            try:
                con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
                try:
                    counts = nova_backup.table_counts(con)
                finally:
                    con.close()
                labels = {"sessions": "chats", "messages": "messages", "memories": "memories", "nova_notes": "notes",
                          "nova_reminders": "reminders", "doc_files": "documents", "task_runs": "tasks",
                          "nova_projects": "projects"}
                add("Database", "Contents", INFO, ", ".join(f"{n} {labels[t]}" for t, n in counts.items()))
            except sqlite3.Error as exc:
                add("Database", "Contents", ERROR, f"Couldn't read: {exc}")

    # Backups
    backups = nova_backup.list_backups(backup_folder)
    if backups:
        newest, when, size = backups[0]
        age_days = (when.now() - when).days
        add("Backups", "Latest", OK if age_days <= 7 else WARNING,
            f"{when:%Y-%m-%d %H:%M} · {newest.name} · {_size(size)} ({len(backups)} in total)")
    else:
        add("Backups", "Latest", WARNING, "No backups yet. Use Back up now.")

    # Logging and settings
    log_path = Path(log_path)
    if log_path.exists():
        add("Logging", "Log file", OK, f"{log_path.name} · {_size(log_path.stat().st_size)} (rotates at 1 MB, keeps 3 old files)")
    else:
        add("Logging", "Log file", INFO, f"{log_path.name} (not written yet)")
    add("Settings", "Settings file", WARNING if settings_warnings else OK,
        "; ".join(settings_warnings) if settings_warnings else "Valid")

    # Components and speech
    for import_name, distribution, purpose, required in COMPONENTS:
        installed, version = component_status(import_name, distribution)
        if installed:
            add("Components", distribution, OK, f"{version or 'installed'} · {purpose}")
        else:
            add("Components", distribution, ERROR if required else WARNING,
                f"Not installed · {purpose}" + ("" if required else f" won't work (pip install {distribution})"))
    for name, finder, missing in (
        ("whisper.cpp (voice input)", find_whisper, "Not found: voice input won't work (see README step 3)"),
        ("Vosk model (wake word)", find_vosk_model, "Not found: “Hey Nova” won't work (see README step 4)"),
    ):
        if finder is None:
            continue
        try:
            found = finder()
            add("Speech", name, OK if found else WARNING, "Found" if found else missing)
        except Exception as exc:
            add("Speech", name, WARNING, f"{missing} ({exc})")
    if count_input_devices is not None:
        try:
            count = count_input_devices()
            add("Speech", "Microphones", OK if count else WARNING,
                f"{count} usable input device{'s' if count != 1 else ''}" if count else "No usable microphone found")
        except Exception as exc:
            add("Speech", "Microphones", WARNING, f"Couldn't list audio devices: {exc}")

    # Optional online integrations (Step 26): status only, never a key
    for name, state, detail in online or ():
        add("Online", name, INFO, f"{'On' if state == 'on' else 'Off'} · {detail}")

    # Performance (this session): timings only, no content
    from nova_system import working_set_mb
    memory = working_set_mb()
    if memory is not None:
        add("Performance", "Nova's memory use", INFO, f"{memory:.0f} MB (Ollama's model memory is separate)")
    emulated = python_is_emulated()
    add("Performance", "Processor", WARNING if emulated else INFO,
        f"{os.cpu_count()} cores · " + ("Python is the x64 build running under emulation on this ARM64 PC: a native "
                                        "ARM64 Python would make Nova's own work faster (see README)."
                                        if emulated else f"Python is native ({platform.machine()})"))
    for name, last, median, count, unit in performance or ():
        shown = f"{last:.0f} {unit}" if unit == "ms" else f"{last:.1f} {unit}"
        if count > 1:
            shown += f" (median {median:.0f} {unit} over {count})" if unit == "ms" else f" (median {median:.1f} over {count})"
        add("Performance", name[0].upper() + name[1:], INFO, shown)
    if not performance:
        add("Performance", "Timings", INFO, "None yet. Chat, speak or index a document, then run the checks again.")
    return checks


def report_text(checks):
    """Plain-text report for copying into a bug report (already redacted)."""
    lines, area = [f"Nova diagnostics · {NOVA_VERSION}"], None
    for check in checks:
        if check.area != area:
            area = check.area
            lines.append(f"\n[{area}]")
        lines.append(f"  {check.status.upper():8} {check.name}: {check.detail}")
    return "\n".join(lines)

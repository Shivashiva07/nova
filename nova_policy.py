"""
Nova's permission model (Step 34). No GUI code.

One place decides what any tool call may do, whether it comes from a typed request, a voice command, an agent
plan, a `task:` run or a scheduled task:

  capability      read          reads information on this PC; never changes anything
                  modify        reversible changes on this PC: opening apps, folders and pages you named,
                                creating or copying files in allowed folders, notes and reminders
                  external      sends something off this PC: search queries, form contents, online lookups
                  consequential deletes, moves, renames, replaces or bulk-changes files, uses the clipboard,
                                closes apps, answers dialogs, submits, buys, posts or changes account settings

  Rules (in code; no setting, plan, prompt or approval can relax them):
  - Every tool must be listed here. Anything else is denied (deny by default).
  - external and consequential calls ask the user right before they run, every time: a plan approval or an
    earlier “yes” never covers them.
  - modify calls need the plan approved (agent), or are the user's explicit command (a typed or spoken
    “open notepad”). A plan approval expires: after APPROVAL_TTL minutes, each further change asks again.
  - A change whose arguments came out of untrusted content (a web page, a document, the screen, an online
    result) asks first: instructions found in content never authorize anything.
  - While new tasks are paused, nothing new starts from any origin (running tasks are stopped separately).
  - There are no tools for software installation, shell commands, code, the registry, system settings,
    purchases or accounts; requests for them are refused before planning.

Decisions (allowed, asked, approved, declined, denied, expired) are logged to approval_log with the task id, step,
tool, origin and time; secrets are redacted from what's stored.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("nova.policy")

READ, MODIFY, EXTERNAL, CONSEQUENTIAL = "read", "modify", "external", "consequential"
CAPABILITY_LABELS = {READ: "reads information", MODIFY: "makes a reversible change on this PC",
                     EXTERNAL: "sends information off this PC", CONSEQUENTIAL: "can delete, replace or change "
                                                                              "things that are hard to undo"}
ORIGINS = ("typed", "voice", "agent", "task", "schedule")
APPROVAL_TTL_MINUTES = 10

# Every tool Nova has, and what it may do. A tool that isn't here can't run.
TOOL_CAPABILITIES = {
    # Step 20 / 25 task actions
    "open_website": MODIFY, "open_app": MODIFY, "open_folder": MODIFY, "open_folder_in_vscode": MODIFY,
    "search_files": READ, "create_note": MODIFY, "schedule_reminder": MODIFY, "switch_app": MODIFY,
    "open_file": MODIFY, "read_document": READ, "create_folder": MODIFY, "create_file": MODIFY,
    "copy_file": MODIFY, "move_file": CONSEQUENTIAL, "rename_file": CONSEQUENTIAL, "recycle_file": CONSEQUENTIAL,
    "copy_text_to_clipboard": CONSEQUENTIAL, "save_clipboard_as_note": CONSEQUENTIAL,
    # Step 28 read-only tools and the online lookup
    "find_memories": READ, "find_notes": READ, "list_reminders": READ, "search_documents": READ,
    "search_chats": READ, "current_time": READ, "online_lookup": EXTERNAL,
    # Step 30 desktop, files and browser
    "list_windows": READ, "focus_window": MODIFY, "read_window": READ, "type_text": MODIFY, "press_keys": MODIFY,
    "click_control": MODIFY, "close_app": CONSEQUENTIAL, "list_folder": READ, "read_text_file": READ,
    "organize_files": CONSEQUENTIAL, "browser_open": MODIFY, "browser_search": EXTERNAL, "browser_read": READ,
    "browser_click": MODIFY, "browser_type": MODIFY, "browser_back": MODIFY, "browser_close": MODIFY,
    # Step 31 screen
    "look_at_screen": READ, "check_screen_for": READ,
}
# Tools whose output is content from outside Nova that could carry instructions (web pages, documents, file text,
# the screen, online results). File paths found on disk aren't: they're the actual files, used as such.
UNTRUSTED_OUTPUT = {"browser_open", "browser_read", "browser_search", "browser_click", "browser_type", "browser_back",
                    "online_lookup", "read_text_file", "read_document", "search_documents", "read_window",
                    "look_at_screen", "check_screen_for"}
# Programs Nova itself may start (by file name); Code.exe must be a full path to an existing file.
LAUNCHABLE = {"notepad.exe", "calc.exe", "mspaint.exe"}


class Denied(Exception):
    """A tool call isn't allowed; the message is shown to the user."""


@dataclass
class Decision:
    allowed: bool
    confirm: str | None = None        # why the user must confirm right before it runs (None: no question)
    reason: str = ""                  # why it was denied


class Gate:
    """The shared switch for pausing new tasks, and the decision for each tool call."""

    def __init__(self):
        self._paused = threading.Event()
        self.approval_ttl = APPROVAL_TTL_MINUTES * 60

    # ---- pausing ----
    @property
    def paused(self):
        return self._paused.is_set()

    def pause(self, on=True):
        (self._paused.set if on else self._paused.clear)()
        log.info("New tasks %s", "paused" if on else "allowed again")

    def check_start(self, origin):
        """Raise Denied if new tasks may not start now."""
        if origin not in ORIGINS:
            raise Denied("A request from an unknown source isn't allowed.")
        if self.paused:
            raise Denied("Nova's tasks are paused, so nothing was started. Resume them from the sidebar or the tray "
                         "(“Resume tasks”).")

    # ---- deciding ----
    @staticmethod
    def capability(name):
        return TOOL_CAPABILITIES.get(name)

    def decide(self, name, origin, untrusted_from=None, already_confirming=None):
        """Whether a call may run, and whether it must ask right before.

        untrusted_from: the tool whose output supplied an argument (e.g. a link taken from a web page).
        already_confirming: the reason the tool itself already asks with, if any."""
        capability = self.capability(name)
        if capability is None:
            return Decision(False, reason=f"“{name}” isn't one of Nova's tools, so it's not allowed.")
        if origin not in ORIGINS:
            return Decision(False, reason="A request from an unknown source isn't allowed.")
        confirm = already_confirming
        if capability in (EXTERNAL, CONSEQUENTIAL) and not confirm:
            confirm = CAPABILITY_LABELS[capability]
        if untrusted_from and capability != READ and not confirm:
            confirm = (f"uses text taken from {describe_source(untrusted_from)}; instructions found in content are "
                       "never followed without you")
        return Decision(True, confirm)

    def approval_expired(self, approved_at, now=None):
        """A plan approval covers changes only for a while; afterwards each change asks again."""
        if approved_at is None:
            return False
        return (now if now is not None else time.monotonic()) - approved_at > self.approval_ttl


GATE = Gate()


def describe_source(tool):
    return {"browser_open": "a web page", "browser_read": "a web page", "browser_search": "search results",
            "browser_click": "a web page", "browser_type": "a web page", "browser_back": "a web page",
            "online_lookup": "an online result", "read_text_file": "a file", "read_document": "a document",
            "search_documents": "a document", "read_window": "an app's window", "look_at_screen": "the screen",
            "check_screen_for": "the screen", "list_folder": "a folder listing",
            "search_files": "file names"}.get(tool, "outside content")


def safety_for(capability):
    """The agent engine's safety class a capability needs at least."""
    import nova_agent as na
    return {READ: na.READ_ONLY, MODIFY: na.REVERSIBLE, EXTERNAL: na.CONSEQUENTIAL,
            CONSEQUENTIAL: na.CONSEQUENTIAL}[capability]


# ==================== processes ====================
def check_launch(argv):
    """Validate a program start: a list (never a shell string), a known program or Code.exe by full path,
    plain string arguments. Raises Denied."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise Denied("Programs are only started from a fixed list of arguments.")
    program = argv[0]
    name = Path(program).name.lower()
    if name in LAUNCHABLE and (program.lower() == name or Path(program).is_file()):
        pass
    elif name == "code.exe" and Path(program).is_absolute() and Path(program).is_file():
        pass
    else:
        raise Denied(f"Nova doesn't start “{Path(program).name}”.")
    for arg in argv[1:]:
        if any(ord(c) < 32 for c in arg) or len(arg) > 1024:
            raise Denied("A program argument isn't valid.")
        if not (Path(arg).is_absolute() and Path(arg).exists()):
            raise Denied("Programs are only given existing folders or files as arguments.")
    return list(argv)


# ==================== the approval log ====================
SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id INTEGER,
    step TEXT,
    tool TEXT,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    origin TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_log_run ON approval_log(run_id, id);
"""
DECISIONS = ("allowed", "asked", "approved", "declined", "denied", "expired", "paused", "stopped")


def log_decision(connect, run_id, step, tool, action, decision, origin=None, detail=None):
    """Record an approval decision (never raises; text is redacted and capped)."""
    if connect is None:
        return
    from nova_system import redact
    try:
        con = connect()
        try:
            with con:
                con.executescript(SCHEMA)
                con.execute("INSERT INTO approval_log (at, run_id, step, tool, action, decision, origin, detail) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (datetime.now().astimezone().isoformat(timespec="seconds"), run_id, step, tool,
                             redact(action, 300), decision, origin, redact(detail, 500) if detail else None))
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("Couldn't record an approval decision: %s", exc)
    log.info("Approval %s: run %s step %s %s (%s)", decision, run_id, step, tool, origin)


def list_decisions(con, limit=300, run_id=None):
    con.executescript(SCHEMA)
    where, args = ("WHERE run_id = ?", (run_id, limit)) if run_id is not None else ("", (limit,))
    return [dict(zip(("at", "run_id", "step", "tool", "action", "decision", "origin", "detail"), r)) for r in
            con.execute(f"SELECT at, run_id, step, tool, action, decision, origin, detail FROM approval_log {where} "
                        "ORDER BY id DESC LIMIT ?", args)]


# ==================== direct commands ====================
_DIRECT = (("open_website", re.compile(r"\b(?:open|launch|go to|visit)\b.*\b(?:youtube|google|gmail|github|chatgpt)\b")),
           ("open_app", re.compile(r"\b(?:open|launch|start)\b.*\b(?:notepad|calculator|paint|visual studio code|"
                                   r"vs code|vscode)\b")),
           ("open_folder", re.compile(r"\b(?:open|show)\b.*\b(?:downloads|documents|desktop|pictures)\b")))


def direct_command_tool(text):
    """The tool a direct chat/voice command corresponds to (“open notepad” → open_app), for the gate."""
    low = str(text or "").lower()
    return next((tool for tool, pattern in _DIRECT if pattern.search(low)), None)

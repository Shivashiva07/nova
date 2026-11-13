"""
Safe multi-step task execution for Nova: planning, a strict action registry,
validation, a sequential runner with confirmation, cancellation and timeouts,
and an execution history. No GUI code.

Safety model
- Every step names an action from REGISTRY and is executed by a fixed Python
  handler. There is no action that takes a command, script, URL, executable
  path or registry key, so nothing a model writes can be run as a command.
- Parameters are validated strictly (fixed choices, length limits, existing
  folders only, no network/device paths); unknown actions or extra fields
  reject the whole plan.
- Which steps need an extra confirmation is decided here, in code, from the
  validated parameters; a model can't mark anything safe.
- The registry has no destructive operations: no deleting, moving or
  overwriting files, no shell, PowerShell, registry, credentials or elevation.
- A step only succeeds when its handler has checked the result (a process
  started and didn't fail, a note/reminder was read back from the database).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ASK = "ask"                 # folder chosen by the user in a dialog before the plan runs
MAX_STEPS = 8
MAX_NOTE_TITLE = 120
MAX_NOTE_CONTENT = 2000
MAX_SEARCH_TERM = 100
DUPLICATE_WINDOW = 120      # seconds: re-running an identical task asks first
CONFIRM_TIMEOUT = 300       # seconds to answer a per-step confirmation
STOP_GRACE = 3              # seconds a timed-out step gets to stop cleanly and say what happened

WEBSITES = {
    "youtube": ("YouTube", "https://www.youtube.com"),
    "google": ("Google", "https://www.google.com"),
    "gmail": ("Gmail", "https://mail.google.com"),
    "github": ("GitHub", "https://github.com"),
    "chatgpt": ("ChatGPT", "https://chatgpt.com"),
}
# Fixed command lines; the VS Code path is found on this PC (Code.exe only).
APPS = {
    "notepad": ("Notepad", ["notepad.exe"]),
    "calculator": ("Calculator", ["calc.exe"]),
    "paint": ("Paint", ["mspaint.exe"]),
    "vscode": ("Visual Studio Code", None),
}
APP_ALIASES = {
    "notepad": "notepad", "calculator": "calculator", "calc": "calculator", "paint": "paint",
    "mspaint": "paint", "vscode": "vscode", "vs code": "vscode", "visual studio code": "vscode", "code": "vscode",
}
KNOWN_FOLDERS = ("downloads", "documents", "desktop", "pictures")


class PlanError(ValueError):
    """A step can't be planned; the message is shown to the user."""


class ForbiddenRequest(PlanError):
    """The request asks for something tasks never do (delete, run commands, ...)."""


class ActionError(RuntimeError):
    """A step failed while running; the message is shown to the user."""


class StepResult(str):
    """A step's verified result message (stored and shown as text), plus structured data
    such as paths or matches that the agent engine (Step 28) can pass to later steps and
    check. Code that treats results as plain strings keeps working."""

    def __new__(cls, message, data=None):
        result = super().__new__(cls, message)
        result.data = dict(data or {})
        return result


# ==================== services (real ones are supplied by nova_gui) ====================
@dataclass
class Services:
    open_url: object            # (url) -> bool
    launch: object              # (argv list) -> process with poll() and pid
    open_folder: object         # (path str) -> None, opens File Explorer
    find_vscode: object         # () -> path to Code.exe or None
    search_files: object        # (root Path, term, seconds, should_stop) -> (matches, timed_out)
    save_note: object           # (title, content) -> note id
    get_note: object            # (note id) -> (id, title, content, ...) or None
    save_reminder: object       # (title, "YYYY-MM-DD HH:MM") -> reminder id
    get_reminder: object        # (reminder id) -> (id, title, due_at, ...) or None
    known_folders: dict = field(default_factory=dict)
    home: Path = field(default_factory=Path.home)


    # Step 25 (optional, so older callers and tests keep working)
    approved_folders: object = None   # () -> folders the user allowed Nova to change files in
    protected: object = None          # () -> folders Nova never touches (system, programs, app data, …)
    open_file: object = None          # (path str) -> None, opens a file with its default app
    switch_window: object = None      # (app key) -> True if a window of that app was brought to the front
    recycle: object = None            # (path str) -> None, moves a file to the Recycle Bin
    clipboard_get: object = None      # () -> text on the clipboard ("" if none)
    clipboard_set: object = None      # (text) -> None
    index_document: object = None     # (path, should_stop) -> (status, passages, detail)


@dataclass
class StepContext:
    services: Services
    cancel: threading.Event                       # set when the user cancels the task
    stop: threading.Event = field(default_factory=threading.Event)  # set when this step times out
    approved: frozenset = frozenset()             # paths the user chose or allowed for this run

    def stopped(self):
        return self.cancel.is_set() or self.stop.is_set()


# ==================== parameter validation ====================
def _text(value, label, limit, required=True, multiline=False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PlanError(f"{label} is missing.")
        return ""
    if not isinstance(value, str):
        raise PlanError(f"{label} must be text.")
    text = value.strip() if multiline else " ".join(value.split())
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise PlanError(f"{label} contains control characters.")
    if len(text) > limit:
        raise PlanError(f"{label} is longer than {limit} characters.")
    return text


def _choice(value, label, options, aliases=None):
    key = " ".join(str(value or "").strip().lower().split())
    key = (aliases or {}).get(key, key)
    if key not in options:
        raise PlanError(f"“{_short(value)}” isn't a supported {label}. Supported: {', '.join(options)}.")
    return key


def _short(value, limit=60):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def check_folder_path(value):
    """An existing local folder as a resolved Path, or PlanError. Never a file."""
    text = str(value or "").strip().strip('"').strip()
    if not text:
        raise PlanError("The folder is missing.")
    if text.startswith(("\\\\", "//")):
        raise PlanError("Network and device paths aren't allowed.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute() or not path.drive:
        raise PlanError("Use a full folder path such as C:\\Projects\\app, or let Nova ask you to choose the folder.")
    if ":" in text[2:]:
        raise PlanError("That isn't a valid folder path.")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise PlanError(f"The folder {text} doesn't exist.") from None
    if str(resolved).startswith("\\\\"):
        raise PlanError("Network and device paths aren't allowed.")
    if not resolved.is_dir():
        raise PlanError(f"{resolved} is a file, not a folder. Nova only opens folders; it never runs files.")
    return resolved


def _folder(value, services, allow_ask):
    """Normalized folder parameter: a known alias, ASK, or a resolved path string."""
    key = str(value or "").strip().lower()
    key = key[:-len(" folder")] if key.endswith(" folder") else key
    if key in KNOWN_FOLDERS:
        return key
    if key == ASK:
        if not allow_ask:
            raise PlanError("Choose a folder before running this step.")
        return ASK
    return str(check_folder_path(value))


def resolve_folder(value, services):
    if value == ASK:
        raise PlanError("Choose a folder before running this step.")
    if value in KNOWN_FOLDERS:
        path = Path(services.known_folders.get(value, services.home / value.title()))
        if not path.is_dir():
            raise PlanError(f"Your {value.title()} folder wasn't found.")
        return path
    return check_folder_path(value)


def folder_label(value):
    if value == ASK:
        return "a folder you choose"
    if value in KNOWN_FOLDERS:
        return value.title()
    return str(value)


def _outside_home(value, services):
    if value in (ASK, *KNOWN_FOLDERS):
        return None
    path = Path(value)
    home = Path(services.home)
    try:
        path.relative_to(home)
        return None
    except ValueError:
        return f"uses a folder outside your user folder ({path})"


def _when(params):
    if params.get("in_minutes"):
        minutes = params["in_minutes"]
        if minutes % 1440 == 0:
            n, unit = minutes // 1440, "day"
        elif minutes % 60 == 0:
            n, unit = minutes // 60, "hour"
        else:
            n, unit = minutes, "minute"
        return f"in {n} {unit}{'s' if n != 1 else ''}"
    return f"at {params['at']}"


def _due(params):
    if params.get("in_minutes"):
        return (datetime.now() + timedelta(minutes=params["in_minutes"])).replace(second=0, microsecond=0)
    return datetime.strptime(params["at"], "%Y-%m-%d %H:%M")


# ==================== actions ====================
# What an action may do, decided in code (never by a model). Shown in the task review.
NONE = "none"                # opens an app, website or folder, or switches windows
READ = "read"                # reads file names or files the user chose or allowed
NOVA = "nova"                # changes Nova's own notes or reminders
CHANGE = "change"            # creates or copies files in a folder the user allowed
DESTRUCTIVE = "destructive"  # moves, renames, replaces or recycles a file: always asks right before
CLIPBOARD = "clipboard"      # reads or replaces the clipboard
PERMISSION_LABELS = {
    NONE: "opens or switches apps, websites and folders",
    READ: "reads files or file names in folders you chose or allowed",
    NOVA: "changes Nova's notes or reminders",
    CHANGE: "creates or copies files in folders you allowed",
    DESTRUCTIVE: "moves, renames, replaces or recycles files (asks right before)",
    CLIPBOARD: "uses the clipboard (asks right before)",
}


@dataclass(frozen=True)
class Action:
    name: str
    title: str
    signature: str              # shown to the planning model and in help
    allowed: frozenset          # permitted parameter names
    timeout: float
    validate: object            # (raw params, services, allow_ask) -> params
    describe: object            # (params) -> str
    sensitive: object           # (params, services) -> reason for a confirmation right before it runs, or None
    run: object                 # (params, ctx) -> verified result message
    permission: str = NONE      # one of PERMISSION_LABELS
    private: frozenset = frozenset()   # parameters never stored in the task history (text, contents)
    paths: object = None        # (params) -> [(path, "read" | "change")] that must be in allowed folders
    alone: bool = False         # a single command with this action is planned as a task (reviewed first)
    preview: object = None      # (params) -> text shown in the review and confirmation only, never stored


LAUNCH_CHECK_SECONDS = 1.5


def _check_started(process, title, ctx):
    """A launched app counts as started if it is still running, or exited cleanly
    (some Windows apps hand off to another process), after a short wait."""
    deadline = time.monotonic() + LAUNCH_CHECK_SECONDS
    while time.monotonic() < deadline and not ctx.stopped():
        code = process.poll()
        if code is not None:
            if code != 0:
                raise ActionError(f"{title} closed immediately (exit code {code}).")
            return
        time.sleep(0.1)


def _launch(ctx, argv, title):
    try:
        process = ctx.services.launch(argv)
    except FileNotFoundError:
        raise ActionError(f"{title} isn't installed on this PC.") from None
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            raise ActionError(f"{title} needs administrator rights; Nova doesn't elevate.") from None
        raise ActionError(f"Windows couldn't start {title}: {exc}") from None
    _check_started(process, title, ctx)
    return process


def _vscode(ctx):
    exe = ctx.services.find_vscode()
    if not exe or Path(exe).name.lower() != "code.exe" or not Path(exe).is_file():
        raise ActionError("Visual Studio Code (Code.exe) wasn't found on this PC.")
    return str(exe)


# ---- validators ----
def _v_site(raw, services, allow_ask):
    return {"site": _choice(raw.get("site"), "website", list(WEBSITES))}


def _v_app(raw, services, allow_ask):
    if str(raw.get("app", "")).strip().lower() in ("task manager", "taskmgr", "cmd", "powershell", "terminal",
                                                    "regedit", "command prompt"):
        raise PlanError(f"“{_short(raw.get('app'))}” can't be started by a task (it can change system settings "
                        "or run commands).")
    return {"app": _choice(raw.get("app"), "app", list(APPS), APP_ALIASES)}


def _v_folder(raw, services, allow_ask):
    return {"folder": _folder(raw.get("folder"), services, allow_ask)}


def _v_search(raw, services, allow_ask):
    term = _text(raw.get("term"), "The filename to search for", MAX_SEARCH_TERM).strip("\"'")
    if not term or any(ch in term for ch in '\\/:'):
        raise PlanError("Search for part of a file name, without folders or drive letters.")
    return {"term": term, "folder": _folder(raw.get("folder") or "documents", services, allow_ask)}


def _v_note(raw, services, allow_ask):
    return {"title": _text(raw.get("title"), "The note title", MAX_NOTE_TITLE),
            "content": _text(raw.get("content"), "The note text", MAX_NOTE_CONTENT, required=False, multiline=True)}


def _v_reminder(raw, services, allow_ask):
    title = _text(raw.get("title"), "The reminder", MAX_NOTE_TITLE)
    minutes, at = raw.get("in_minutes"), raw.get("at")
    if (minutes in (None, "")) == (at in (None, "")):
        raise PlanError("A reminder needs either a delay in minutes or a date and time, not both.")
    if minutes not in (None, ""):
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float, str)):
            raise PlanError("The reminder delay must be a number of minutes.")
        try:
            minutes = int(float(minutes))
        except ValueError:
            raise PlanError("The reminder delay must be a number of minutes.") from None
        if not 1 <= minutes <= 365 * 24 * 60:
            raise PlanError("Reminders can be between 1 minute and 1 year away.")
        return {"title": title, "in_minutes": minutes}
    try:
        due = datetime.strptime(str(at).strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise PlanError("Use YYYY-MM-DD HH:MM for the reminder time, e.g. 2026-11-08 09:30.") from None
    if due <= datetime.now():
        raise PlanError("The reminder time is in the past.")
    if due > datetime.now() + timedelta(days=366):
        raise PlanError("Reminders can be at most a year away.")
    return {"title": title, "at": due.strftime("%Y-%m-%d %H:%M")}


# ---- handlers (each verifies its result) ----
def _r_site(params, ctx):
    title, url = WEBSITES[params["site"]]
    if not ctx.services.open_url(url):
        raise ActionError("Windows couldn't open your default web browser.")
    return StepResult(f"Asked your default browser to open {title}.", {"url": url, "site": params["site"]})


def _r_app(params, ctx):
    title, argv = APPS[params["app"]]
    process = _launch(ctx, [_vscode(ctx)] if params["app"] == "vscode" else list(argv), title)
    return StepResult(f"Started {title} (process {getattr(process, 'pid', '?')}).",
                      {"app": params["app"], "pid": getattr(process, "pid", None)})


def _r_folder(params, ctx):
    path = resolve_folder(params["folder"], ctx.services)
    try:
        ctx.services.open_folder(str(path))
    except OSError as exc:
        raise ActionError(f"Windows couldn't open {path}: {exc}") from None
    return StepResult(f"Opened {path} in File Explorer.", {"folder": str(path)})


def _r_vscode_folder(params, ctx):
    path = resolve_folder(params["folder"], ctx.services)
    process = _launch(ctx, [_vscode(ctx), str(path)], "Visual Studio Code")
    return StepResult(f"Opened {path} in Visual Studio Code (process {getattr(process, 'pid', '?')}).",
                      {"folder": str(path), "pid": getattr(process, "pid", None)})


def _r_search(params, ctx):
    root = resolve_folder(params["folder"], ctx.services)
    matches, timed_out = ctx.services.search_files(root, params["term"], 20, ctx.stopped)
    if ctx.stopped():
        raise ActionError("The search was stopped.")
    note = " (stopped after 20 seconds; some folders weren't checked)" if timed_out else ""
    if not matches:
        return StepResult(f"No file names containing “{params['term']}” in {root}{note}.",
                          {"folder": str(root), "matches": [], "timed_out": timed_out})
    listing = "\n".join(f"  • {path}" for path in matches)
    return StepResult(f"Found {len(matches)} file(s) containing “{params['term']}” in {root}{note}:\n{listing}",
                      {"folder": str(root), "matches": [str(p) for p in matches], "timed_out": timed_out})


def _r_note(params, ctx):
    note_id = ctx.services.save_note(params["title"], params["content"])
    saved = ctx.services.get_note(note_id)
    if not saved or saved[1] != params["title"]:
        raise ActionError("The note couldn't be confirmed in the database.")
    return StepResult(f"Saved note #{note_id}: {params['title']}.", {"note_id": note_id})


def _r_reminder(params, ctx):
    due = _due(params)
    if due <= datetime.now():
        raise ActionError("The reminder time has already passed.")
    due_text = due.strftime("%Y-%m-%d %H:%M")
    reminder_id = ctx.services.save_reminder(params["title"], due_text)
    saved = ctx.services.get_reminder(reminder_id)
    if not saved or saved[1] != params["title"] or saved[2] != due_text:
        raise ActionError("The reminder couldn't be confirmed in the database.")
    return StepResult(f"Reminder #{reminder_id} set for {due_text}: {params['title']}.",
                      {"reminder_id": reminder_id, "due": due_text})


def _never(params, services):
    return None


def _folder_sensitivity(params, services):
    return _outside_home(params["folder"], services)


REGISTRY = {action.name: action for action in (
    Action("open_website", "Open a website", "open_website(site: youtube|google|gmail|github|chatgpt)",
           frozenset({"site"}), 10, _v_site,
           lambda p: f"Open {WEBSITES[p['site']][0]} in your browser", _never, _r_site),
    Action("open_app", "Start an app", "open_app(app: notepad|calculator|paint|vscode)",
           frozenset({"app"}), 15, _v_app, lambda p: f"Start {APPS[p['app']][0]}", _never, _r_app),
    Action("open_folder", "Open a folder in File Explorer",
           "open_folder(folder: downloads|documents|desktop|pictures|ask|<full path the user gave>)",
           frozenset({"folder"}), 10, _v_folder, lambda p: f"Open {folder_label(p['folder'])} in File Explorer",
           _folder_sensitivity, _r_folder),
    Action("open_folder_in_vscode", "Open a folder in VS Code",
           "open_folder_in_vscode(folder: downloads|documents|desktop|pictures|ask|<full path the user gave>)",
           frozenset({"folder"}), 20, _v_folder, lambda p: f"Open {folder_label(p['folder'])} in VS Code",
           _folder_sensitivity, _r_vscode_folder),
    Action("search_files", "Search a folder for file names",
           "search_files(term: part of a file name, folder: downloads|documents|desktop|pictures|ask|<full path>)",
           frozenset({"term", "folder"}), 25, _v_search,
           lambda p: f"Search {folder_label(p['folder'])} for file names containing “{p['term']}”",
           _folder_sensitivity, _r_search, permission=READ),
    Action("create_note", "Create a note", "create_note(title: text, content: text)",
           frozenset({"title", "content"}), 5, _v_note,
           lambda p: f"Create the note “{p['title']}”" + (f": {_short(p['content'], 80)}" if p["content"] else ""),
           _never, _r_note, permission=NOVA, private=frozenset({"content"})),
    Action("schedule_reminder", "Schedule a reminder",
           "schedule_reminder(title: text, in_minutes: number) or schedule_reminder(title: text, at: 'YYYY-MM-DD HH:MM')",
           frozenset({"title", "in_minutes", "at"}), 5, _v_reminder,
           lambda p: f"Remind you “{p['title']}” {_when(p)}", _never, _r_reminder, permission=NOVA),
)}


def register(action):
    """Add an action to the registry (Step 25's computer actions live in nova_actions.py)."""
    if action.name in REGISTRY:
        raise ValueError(f"Action {action.name} is already registered.")
    if action.permission not in PERMISSION_LABELS:
        raise ValueError(f"Action {action.name} has an unknown permission level.")
    REGISTRY[action.name] = action
    return action


# Extra clause parsers: (text, context) -> [(action, params)] or None if the clause isn't theirs.
CLAUSE_PARSERS = []


# ==================== plans ====================
@dataclass
class Step:
    action: str
    params: dict
    description: str = ""
    sensitive: str | None = None    # reason for an extra confirmation, decided in code
    status: str = "pending"
    result: str | None = None
    error: str | None = None
    unapproved: list = field(default_factory=list)   # folders this step needs that the user hasn't allowed
    preview: str | None = None      # e.g. the text to copy: shown before running, never stored


# Parameters the user can fill in with a dialog (value ASK): param -> "folder" or "file"
PICK_PARAMS = {"folder": "folder", "destination": "folder", "file": "file"}


@dataclass
class Plan:
    request: str
    steps: list
    planner: str = "rules"          # "rules" (deterministic parser) or "model" (proposed by the local LLM)
    problems: list = field(default_factory=list)
    picked: set = field(default_factory=set)   # folders and files the user chose or allowed for this run

    @property
    def runnable(self):
        return bool(self.steps) and not self.problems

    @property
    def auto_run(self):
        """A single harmless step Nova understood itself (e.g. “switch to notepad”): no review needed."""
        if len(self.steps) != 1 or self.planner != "rules" or not self.runnable:
            return False
        step = self.steps[0]
        action = REGISTRY.get(step.action)
        return bool(action and action.alone and action.permission == NONE and not step.sensitive
                    and not step.unapproved)

    def needs_folder(self):
        return [i for i, s in enumerate(self.steps) if s.params.get("folder") == ASK]

    def needs_pick(self):
        """[(step index, parameter, "folder" | "file")] still to be chosen by the user."""
        return [(i, name, kind) for i, s in enumerate(self.steps) for name, kind in PICK_PARAMS.items()
                if s.params.get(name) == ASK]

    def needs_approval(self):
        """Folders some step needs that aren't allowed yet, in order."""
        seen = []
        for step in self.steps:
            for folder in step.unapproved:
                if folder not in seen:
                    seen.append(folder)
        return seen


def validate_plan(plan, services, allow_ask=True):
    """Check every step against the registry; fill descriptions and confirmation needs.

    Any unknown action, unexpected field or invalid value is recorded in
    plan.problems, and a plan with problems can't run.
    """
    problems = [p for p in plan.problems if not p.startswith("Step ")]
    if len(plan.steps) > MAX_STEPS:
        problems.append(f"A task can have at most {MAX_STEPS} steps.")
    for number, step in enumerate(plan.steps, start=1):
        action = REGISTRY.get(step.action)
        if action is None:
            problems.append(f"Step {number}: “{_short(step.action)}” isn't an action Nova can perform.")
            continue
        if not isinstance(step.params, dict):
            problems.append(f"Step {number}: its details are malformed.")
            continue
        extra = set(step.params) - action.allowed
        if extra:
            problems.append(f"Step {number}: unexpected detail(s) {', '.join(sorted(map(str, extra)))}.")
            continue
        try:
            step.params = action.validate(step.params, services, allow_ask)
        except PlanError as exc:
            problems.append(f"Step {number} ({action.title.lower()}): {exc}")
            continue
        step.description = action.describe(step.params)
        step.preview = action.preview(step.params) if action.preview else None
        reason = action.sensitive(step.params, services)
        if not reason and plan.planner == "model":
            suggested = [step.params.get(name) for name in PICK_PARAMS
                         if step.params.get(name) not in (None, ASK, *KNOWN_FOLDERS)
                         and step.params.get(name) not in plan.picked]
            if suggested:
                reason = ("uses a folder path suggested by the AI model" if set(step.params) & {"folder"}
                          and not set(step.params) & {"file", "destination"} else "uses a path suggested by the AI model")
        step.sensitive = reason
        step.unapproved = []
        if action.paths is not None:
            for path, need in action.paths(step.params):
                root = approval_needed(path, need, services, plan.picked)
                if root and root not in step.unapproved:
                    step.unapproved.append(root)
            if step.unapproved and not allow_ask:
                problems.append(f"Step {number} ({action.title.lower()}): Nova isn't allowed to "
                                f"{'change' if action.permission != READ else 'open'} files in "
                                f"{', '.join(step.unapproved)}.")
    plan.problems = problems
    return plan


def _inside(path, root):
    path, root = os.path.normcase(str(path)), os.path.normcase(str(root)).rstrip("\\/")
    return path == root or path.startswith(root + os.sep)


def approval_needed(path, need, services, picked=()):
    """None if the user allowed `path` for `need` ("read" or "change"); otherwise the folder to ask about.

    Allowed: folders listed in Tasks › Allowed folders, and folders or files the
    user chose (or allowed) for this run. Files in Downloads, Documents, Desktop
    and Pictures may also be opened or read.
    """
    roots = list(picked)
    roots += [str(p) for p in (services.approved_folders() if services.approved_folders else [])]
    if need == "read":
        roots += [str(p) for p in services.known_folders.values()]
    if any(_inside(path, root) for root in roots):
        return None
    return str(path) if os.path.isdir(path) else str(Path(path).parent)


def plan_hash(plan):
    canonical = json.dumps([[s.action, s.params] for s in plan.steps], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ==================== deterministic planner ====================
_VERBS = (r"(?:open|launch|start|run|search|find|look|locate|create|make|add|write|save|remind|schedule|set"
          r"|delete|remove|erase|wipe|format|move|rename|copy|overwrite|install|uninstall|execute|kill|shut|restart"
          r"|reboot|change|edit|modify|disable|enable|download|upload|send|email|type|paste"
          r"|switch|bring|focus|read|index|import|recycle|trash)")
_FORBIDDEN = re.compile(
    r"^(?:erase|wipe|format|overwrite|install|uninstall|execute|kill|shut|restart"
    r"|reboot|change|edit|modify|disable|enable|download|upload|send|email|type|paste)\b"
    # Step 25 file actions work on one file at a time, never on whole folders or "everything".
    r"|^(?:delete|remove|recycle|trash|move|copy|rename)\s+(?:all\s+|every\s+|the\s+|my\s+|this\s+|that\s+|these\s+"
    r"|those\s+|whole\s+|entire\s+)*(?:everything|all\b|files\b|folders?\b|director(?:y|ies)\b|downloads\b|documents\b"
    r"|desktop\b|pictures\b|temp\b|temporary\b|drive\b|disk\b|[A-Za-z]:\\?\s*$|[A-Za-z]:\\?\s+(?:to|into)\b)"
    r"|\b(?:powershell|cmd(?:\.exe)?|command\s+prompt|terminal|shell|scripts?|registry|regedit|passwords?"
    r"|credentials?|administrator|admin\s+rights|elevated?|elevation|sudo|batch\s+file)\b"
    r"|\.(?:bat|cmd|ps1|exe|vbs|js|msi)\b",
    re.IGNORECASE,
)
_SPLIT = re.compile(
    r"\s*;\s*|,?\s+and\s+then\s+|,?\s+then\s+|,?\s+after\s+that,?\s+|,?\s+and\s+also\s+|,?\s+next,?\s+"
    rf"|,?\s+and\s+(?={_VERBS}\b)|,\s*(?={_VERBS}\b)",
    re.IGNORECASE,
)
_EXPLICIT = re.compile(r"^\s*(?:/task\b|task\s*:|plan\s*:)\s*(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
_UNITS = {"min": 1, "minute": 1, "minutes": 1, "mins": 1, "hour": 60, "hours": 60, "hr": 60, "hrs": 60,
          "day": 1440, "days": 1440}
_WHEN = r"(?:in\s+(?P<n>\d{1,6})\s+(?P<unit>minutes?|mins?|hours?|hrs?|days?)|(?:at|on)\s+(?P<at>\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}))"


def explicit_request(text):
    """'task: …' or '/task …' -> the request; otherwise None."""
    match = _EXPLICIT.match(str(text or ""))
    return match.group("rest").strip() if match else None


def _folder_expr(text, context):
    """Interpret a folder phrase: alias, 'a folder I choose', a path, or 'it/there'."""
    t = re.sub(r"\s+in\s+(?:the\s+)?(?:file\s+)?explorer$", "", text.strip().strip(".").strip(), flags=re.IGNORECASE)
    low = t.lower()
    low = re.sub(r"^(?:in\s+|inside\s+)", "", low)
    low = re.sub(r"^(?:my|the|a|an)\s+", "", low)
    if low in ("it", "there", "that folder", "this folder", "the folder", "that", "same folder"):
        if context.get("folder"):
            return context["folder"]
        raise PlanError("It isn't clear which folder “it” refers to.")
    stripped = re.sub(r"\s+folder$", "", low)
    if stripped in KNOWN_FOLDERS or stripped == "download":
        return "downloads" if stripped == "download" else stripped
    if re.fullmatch(r"(?:(?:selected|chosen|specific|some|particular|project|code|any|new)\s+)*folder"
                    r"(?:\s+(?:i|you)\s+(?:choose|select|pick))?(?:\s+of\s+my\s+choice)?", low):
        return ASK
    path = re.sub(r"^(?:the\s+|my\s+)?(?:folder|directory)\s+", "", t, flags=re.IGNORECASE).strip().strip('"')
    if re.match(r"^[A-Za-z]:[\\/]", path):
        return path
    return None


def _parse_clause(clause, context):
    """One clause -> list of (action, params). Raises PlanError if not understood."""
    text = re.sub(r"^(?:please\s+|nova[,:]?\s+|also\s+|then\s+|and\s+)+", "", clause.strip(), flags=re.I)
    text = text.strip().rstrip(".!")
    low = text.lower()
    if _FORBIDDEN.search(low):
        raise ForbiddenRequest(f"“{_short(clause)}”: tasks never run commands, scripts, PowerShell or the "
                               "registry, never install or change settings, and only copy, move, rename or "
                               "recycle single files in folders you allow (never whole folders).")
    for parser in CLAUSE_PARSERS:     # Step 25: file, clipboard and window actions (nova_actions.py)
        steps = parser(text, context)
        if steps is not None:
            return steps

    # Open a folder in VS Code.
    m = re.fullmatch(r"(?:open|load)\s+(?P<what>.+?)\s+in\s+(?:vs\s?code|vscode|visual studio code|code)", low)
    if m:
        folder = _folder_expr(text[m.start("what"):m.end("what")], context)
        if folder is None:
            raise PlanError(f"“{_short(m.group('what'))}” isn't a folder Nova can open.")
        context["folder"] = folder
        return [("open_folder_in_vscode", {"folder": folder})]

    # Note.
    m = re.fullmatch(r"(?:create|make|add|write|save)\s+(?:a\s+)?(?:new\s+)?note\s+(?:called|titled|named)\s+"
                     r"(?P<title>.+?)(?:\s*(?:saying|that\s+says|with\s+the\s+text|with|:)\s+(?P<content>.+))?",
                     text, re.I | re.S)
    if not m:
        m = re.fullmatch(r"(?:save\s+)?note\s*:\s*(?P<title>[^|]+?)\s*\|\s*(?P<content>.+)", text, re.I | re.S)
    if m:
        title = m.group("title").strip().strip("\"'“”")
        context["note"] = title
        return [("create_note", {"title": title, "content": (m.group("content") or "").strip()})]

    # Reminder about the note created earlier ("remind me about it in 30 minutes").
    m = re.fullmatch(rf"(?:remind\s+me\s+(?:about|of)\s+(?:it|this|that|the\s+note)|(?:schedule|set|add|create)\s+"
                     rf"(?:a\s+)?reminder\s+(?:for|about)\s+(?:it|this|that|the\s+note))\s+{_WHEN}", low)
    if m:
        if not context.get("note"):
            raise PlanError("There's no note earlier in the task for the reminder to refer to.")
        return [("schedule_reminder", _when_params(m, f"Note: {context['note']}"))]

    m = re.fullmatch(rf"remind\s+me\s+(?:to\s+|about\s+)?(?P<title>.+?)\s+{_WHEN}", text, re.I)
    if m:
        return [("schedule_reminder", _when_params(m, m.group("title").strip()))]

    # File search: needs a file word, a folder, or a folder opened earlier ("search it for").
    m = re.fullmatch(
        r"(?:search|find|look|locate)(?:\s+(?P<pre>it|there|in\s+it|that\s+folder|in\s+there))?\s*(?:for\s+)?"
        r"(?P<files>(?:a\s+|the\s+|my\s+)?files?\s+(?:named|called|containing)\s+)?(?P<term>.+?)"
        r"(?:\s+in\s+(?P<where>.+))?", text, re.I)
    if m and (m.group("pre") or m.group("files") or m.group("where")):
        where = m.group("where") or m.group("pre")
        folder = _folder_expr(where, context) if where else context.get("folder", "documents")
        if folder is None:
            raise PlanError(f"“{_short(where)}” isn't a folder Nova can search.")
        term = m.group("term").strip().strip("\"'“”")
        return [("search_files", {"term": term, "folder": folder})]

    # Open websites, apps or folders ("open notepad, calculator and paint").
    m = re.fullmatch(r"(?:open|launch|start|run|go\s+to|visit)\s+(?P<rest>.+)", text, re.I)
    if m:
        rest = m.group("rest").strip()
        folder = _folder_expr(rest, context)
        if folder is not None:
            in_code = context.get("app") == "vscode" and (folder == ASK or "project" in rest.lower())
            context["folder"] = folder
            return [("open_folder_in_vscode" if in_code else "open_folder", {"folder": folder})]
        items = [i.strip() for i in re.split(r",\s*|\s+and\s+|\s*&\s*", rest) if i.strip()]
        steps = []
        for item in items:
            name = re.sub(r"^(?:the|my)\s+", "", item.lower()).strip()
            name = re.sub(r"\s+(?:app|website|site)$", "", name)
            if name in WEBSITES:
                steps.append(("open_website", {"site": name}))
            elif name in APP_ALIASES:
                context["app"] = APP_ALIASES[name]
                steps.append(("open_app", {"app": APP_ALIASES[name]}))
            else:
                raise PlanError(f"Nova doesn't know how to open “{_short(item)}”.")
        return steps

    raise PlanError(f"“{_short(clause)}” isn't something Nova can do in a task.")


def _when_params(match, title):
    if match.group("at"):
        return {"title": title, "at": match.group("at").replace("T", " ")}
    unit = match.group("unit").lower()
    return {"title": title, "in_minutes": int(match.group("n")) * _UNITS.get(unit, _UNITS.get(unit.rstrip("s"), 1))}


def plan_with_rules(text):
    """Deterministic parse into steps. Returns (steps, problems)."""
    context, steps, problems = {}, [], []
    # Quoted parts (usually paths, which may contain "and" or commas) are never split.
    quoted = []

    def hide(match):
        quoted.append(match.group(0))
        return f"\x00{len(quoted) - 1}\x00"

    hidden = re.sub(r"\"[^\"\n]*\"|“[^”\n]*”", hide, str(text).strip())
    for part in (c for c in _SPLIT.split(hidden) if c and c.strip()):
        clause = re.sub(r"\x00(\d+)\x00", lambda m: quoted[int(m.group(1))], part)
        try:
            steps.extend(Step(action, params) for action, params in _parse_clause(clause, context))
        except ForbiddenRequest as exc:
            problems.append(FORBIDDEN_PREFIX + str(exc))
        except PlanError as exc:
            problems.append(str(exc))
    return steps, problems


FORBIDDEN_PREFIX = "Not allowed: "


# ==================== model-assisted planner ====================
PLANNER_PROMPT = """You turn a request into a plan for Nova's local task runner.
You may ONLY use these actions, exactly as written:
{actions}

Folder values: downloads, documents, desktop, pictures, "ask" (Nova asks the user to choose a folder), or a full Windows folder path that appears in the request.
File values: "ask" (Nova asks the user to choose the file) or a full Windows file path that appears in the request.
Return JSON only, in this shape:
{{"steps": [{{"action": "<action name>", "params": {{...}}}}], "unsupported": ["<parts of the request none of the actions can do>"]}}
Rules:
- Never invent actions or parameters. There are no actions for shell commands, PowerShell, scripts, running programs or files, installing, editing file contents, whole folders, settings, the registry, passwords or administrator rights; list such requests under "unsupported".
- Only use paths that appear in the request, or "ask". Never guess a path.
- Never substitute a different action for something unsupported (for example, don't create a note when asked to set an alarm or send a message); list that part under "unsupported" instead.
- Keep the user's order. At most {max_steps} steps.
- If everything is supported, "unsupported" is an empty list. If nothing fits, "steps" is an empty list."""


def planner_prompt():
    actions = "\n".join(f"- {a.signature}: {a.title}" for a in REGISTRY.values())
    return PLANNER_PROMPT.format(actions=actions, max_steps=MAX_STEPS)


def plan_with_model(request, chat, model, services):
    """Ask the local model to propose a plan, then validate it like any other.

    The model's output is untrusted: it can only name registry actions and
    parameters, everything is re-validated, and it can't affect which steps
    need confirmation.
    """
    plan = Plan(request, [], "model")
    try:
        response = chat(model=model, messages=[{"role": "system", "content": planner_prompt()},
                                               {"role": "user", "content": request}],
                        format="json", options={"temperature": 0, "num_ctx": 4096})
        content = response["message"]["content"] if isinstance(response, dict) else response.message.content
    except Exception as exc:
        plan.problems.append(f"The local model couldn't be reached to plan this ({exc}).")
        return plan
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        plan.problems.append("The model didn't return a valid plan, so nothing will run.")
        return plan
    if not isinstance(data, dict) or not isinstance(data.get("steps", []), list):
        plan.problems.append("The model's plan was malformed, so nothing will run.")
        return plan
    for raw in data.get("steps", [])[:MAX_STEPS + 1]:
        if not isinstance(raw, dict) or set(raw) - {"action", "params"}:
            plan.problems.append("The model's plan contained an unexpected field, so nothing will run.")
            continue
        plan.steps.append(Step(str(raw.get("action", "")), raw.get("params") or {}))
    unsupported = data.get("unsupported") or []
    if isinstance(unsupported, list):
        for item in unsupported[:5]:
            text = " ".join(str(item).split()) if isinstance(item, str) else ""
            # Small models sometimes echo the prompt's placeholder or "none".
            if (len(text) < 3 or set(text) <= set(".…-_ ") or text.startswith("<")
                    or text.lower().rstrip(".") in ("none", "n/a", "nothing", "null", "no")):
                continue
            plan.problems.append(f"Not supported: {_short(text, 150)}")
    if not plan.steps and not plan.problems:
        plan.problems.append("Nothing in the request matches an action Nova can perform.")
    return validate_plan(plan, services)


def plan_request(text, services, chat=None, model=None):
    """Plan a chat message, or return None if it isn't a multi-step task.

    - "task: …" / "/task …": always planned (rules first, then the model).
    - Otherwise only messages the rules fully understand as 2+ steps
      ("open notepad and then calculator") become tasks; single commands keep
      their existing handling and everything else goes to normal chat.
    """
    explicit = explicit_request(text)
    request = explicit if explicit is not None else str(text or "").strip()
    if explicit is not None and not request:
        return validate_plan(Plan("", [], problems=["Describe the task after “task:”."]), services)
    steps, problems = plan_with_rules(request)
    # File, clipboard and window actions are planned (and reviewed) even as a single command.
    alone = len(steps) == 1 and REGISTRY.get(steps[0].action) is not None and REGISTRY[steps[0].action].alone
    if not problems and (len(steps) >= 2 or alone or (explicit is not None and steps)):
        return validate_plan(Plan(request, steps, "rules"), services)
    if explicit is None:
        return None
    forbidden = [p for p in problems if p.startswith(FORBIDDEN_PREFIX)]
    if forbidden:  # refused in code; the model is never asked to find a way around it
        return validate_plan(Plan(request, steps, "rules", forbidden), services)
    if chat is None:
        return validate_plan(Plan(request, [], "rules", problems or ["Nova couldn't turn that into steps."]),
                             services)
    return plan_with_model(request, chat, model, services)


def supported_summary():
    return "\n".join(f"• {a.title}" for a in REGISTRY.values())


# ==================== history (SQLite) ====================
SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    planner TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_hash ON task_runs(plan_hash, finished_at);
CREATE TABLE IF NOT EXISTS task_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL,
    description TEXT NOT NULL,
    sensitive TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_steps_run ON task_steps(run_id, position);
"""
FINAL = ("succeeded", "failed", "cancelled", "timed_out", "interrupted", "rejected")


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Step 28 (agent engine): extra, optional details per step. Additive; NULL for Step 20 tasks.
AGENT_STEP_COLUMNS = {
    "step_key": "TEXT",          # the plan's id for the step ("s1")
    "depends_on": "TEXT",        # JSON list of step ids
    "condition": "TEXT",         # JSON condition, e.g. {"step": "s1", "status": "failed"}
    "attempts": "INTEGER",       # how many times it was tried (retries)
    "safety": "TEXT",            # read_only | reversible | consequential
    "evidence": "TEXT",          # how the result was checked
    "output": "TEXT",            # a short summary of the output (never file or clipboard contents)
}


def initialize_schema(con):
    """Create the task history tables, and close out runs left 'running' by a crash."""
    con.executescript(SCHEMA)
    present = {row[1] for row in con.execute("PRAGMA table_info(task_steps)")}
    for column, kind in AGENT_STEP_COLUMNS.items():
        if column not in present:
            con.execute(f"ALTER TABLE task_steps ADD COLUMN {column} {kind}")
    if "origin" not in {row[1] for row in con.execute("PRAGMA table_info(task_runs)")}:
        con.execute("ALTER TABLE task_runs ADD COLUMN origin TEXT")   # Step 34: typed, voice, agent, task, schedule
    con.commit()
    with con:
        con.execute("UPDATE task_steps SET status = 'interrupted', finished_at = ? "
                    "WHERE status IN ('running', 'pending') AND run_id IN "
                    "(SELECT id FROM task_runs WHERE status = 'running')", (_now(),))
        con.execute("UPDATE task_runs SET status = 'interrupted', finished_at = ?, "
                    "summary = 'Nova closed while this task was running.' WHERE status = 'running'", (_now(),))


def create_run(con, plan, session_id=None, status="planned"):
    """Record a plan: 'planned' (about to run), or 'cancelled'/'rejected' (never ran)."""
    summary = "Not run: " + " ".join(plan.problems) if status == "rejected" else (
        "You cancelled this task before it ran." if status == "cancelled" else None)
    with con:
        run_id = con.execute(
            "INSERT INTO task_runs (request, planner, plan_hash, status, session_id, created_at, finished_at, "
            "summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (plan.request, plan.planner, plan_hash(plan), status, session_id, _now(),
             _now() if status in FINAL else None, summary),
        ).lastrowid
        con.executemany(
            "INSERT INTO task_steps (run_id, position, action, params, description, sensitive, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, i, s.action, json.dumps(history_params(s), ensure_ascii=False), s.description, s.sensitive,
              "cancelled" if status == "cancelled" else "pending") for i, s in enumerate(plan.steps)],
        )
    return run_id


def history_params(step):
    """A step's parameters as stored in the history: texts and contents are replaced by their length."""
    action = REGISTRY.get(step.action)
    private = action.private if action else frozenset()
    if not isinstance(step.params, dict):
        return {}
    return {key: (f"[{len(str(value))} characters, not stored]" if key in private and value else value)
            for key, value in step.params.items()}


def claim_run(con, run_id):
    """Atomically move a run from planned to running; False if it already ran."""
    with con:
        cursor = con.execute("UPDATE task_runs SET status = 'running', started_at = ? "
                             "WHERE id = ? AND status = 'planned'", (_now(), run_id))
    return cursor.rowcount == 1


def update_step(con, run_id, position, status, result=None, error=None):
    column = "started_at" if status == "running" else "finished_at"
    with con:
        con.execute(f"UPDATE task_steps SET status = ?, result = ?, error = ?, {column} = ? "
                    "WHERE run_id = ? AND position = ?", (status, result, error, _now(), run_id, position))


def finish_run(con, run_id, status, summary):
    with con:
        con.execute("UPDATE task_runs SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
                    (status, summary, _now(), run_id))


def recent_duplicate(con, plan, within=DUPLICATE_WINDOW):
    """Seconds since an identical task last succeeded, if within the window."""
    row = con.execute("SELECT finished_at FROM task_runs WHERE plan_hash = ? AND status = 'succeeded' "
                      "ORDER BY id DESC LIMIT 1", (plan_hash(plan),)).fetchone()
    if not row or not row[0]:
        return None
    age = (datetime.now().astimezone() - datetime.fromisoformat(row[0])).total_seconds()
    return int(age) if age <= within else None


def list_runs(con, limit=50):
    runs = []
    for row in con.execute("SELECT id, request, planner, status, created_at, started_at, finished_at, summary "
                           "FROM task_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        steps = con.execute("SELECT position, action, description, sensitive, status, result, error, attempts, "
                            "evidence FROM task_steps WHERE run_id = ? ORDER BY position", (row[0],)).fetchall()
        runs.append({
            "id": row[0], "request": row[1], "planner": row[2], "status": row[3], "created_at": row[4],
            "started_at": row[5], "finished_at": row[6], "summary": row[7],
            "steps": [{"position": s[0], "action": s[1], "description": s[2], "sensitive": s[3],
                       "status": s[4], "result": s[5], "error": s[6], "attempts": s[7], "evidence": s[8]}
                      for s in steps],
        })
    return runs


# ==================== runner ====================
STEP_ICONS = {"succeeded": "✓", "failed": "✗", "timed_out": "⏱", "skipped": "–", "cancelled": "–",
              "declined": "–", "pending": "·", "running": "…", "interrupted": "!"}


class AlreadyRunning(RuntimeError):
    pass


class TaskRunner:
    """Runs one validated plan at a time, step by step.

    connect() returns a new sqlite3 connection. on_update(run_id, position,
    status, text) is called from the worker thread as steps progress.
    confirm(step) is called for sensitive steps and must return True to proceed.
    """

    def __init__(self, connect, services, on_update=None):
        self._connect = connect
        self.services = services
        self._on_update = on_update or (lambda *args: None)
        self._lock = threading.Lock()

    @property
    def busy(self):
        return self._lock.locked()

    def run(self, run_id, plan, cancel, confirm, timeouts=None):
        """Execute a plan already stored as run_id. Returns (status, summary)."""
        import nova_policy
        try:                                      # Step 34: nothing new starts while tasks are paused
            nova_policy.GATE.check_start("task")
        except nova_policy.Denied as exc:
            raise AlreadyRunning(str(exc)) from None
        if not self._lock.acquire(blocking=False):
            raise AlreadyRunning("Another task is already running.")
        con = self._connect()
        try:
            if not claim_run(con, run_id):
                raise AlreadyRunning("This task has already been run or cancelled.")
            # Re-validate at execution time: folders may have changed since planning,
            # and a plan that fails validation never runs any step.
            validate_plan(plan, self.services, allow_ask=False)
            if not plan.runnable:
                for position, step in enumerate(plan.steps):
                    self._set(con, run_id, position, step, "skipped",
                              error="Not run: the task didn't pass Nova's safety checks.")
                reasons = plan.problems or ["The plan has no steps."]
                summary = "**Task not run**: it didn't pass Nova's safety checks.\n" + "\n".join(
                    f"- {reason}" for reason in reasons)
                finish_run(con, run_id, "rejected", summary)
                return "rejected", summary
            status = "succeeded"
            for position, step in enumerate(plan.steps):
                if status != "succeeded":
                    self._set(con, run_id, position, step, "cancelled" if status == "cancelled" else "skipped",
                              error="Not run: " + ("the task was cancelled." if status == "cancelled"
                                                   else "an earlier step didn't succeed."))
                    continue
                if cancel.is_set():
                    status = "cancelled"
                    self._set(con, run_id, position, step, "cancelled", error="Not run: the task was cancelled.")
                    continue
                import nova_policy                 # Step 34: the same permission model as the agent
                decision = nova_policy.GATE.decide(step.action, "task", already_confirming=step.sensitive)
                if not decision.allowed:
                    status = "failed"
                    nova_policy.log_decision(self._connect, run_id, str(position + 1), step.action, step.description,
                                             "denied", "task", decision.reason)
                    self._set(con, run_id, position, step, "failed", error=f"Not allowed: {decision.reason}")
                    continue
                step.sensitive = decision.confirm
                if step.sensitive:
                    approved = confirm(step)
                    nova_policy.log_decision(self._connect, run_id, str(position + 1), step.action, step.description,
                                             "approved" if approved else "declined", "task", step.sensitive)
                    if not approved:
                        status = "cancelled"
                        self._set(con, run_id, position, step, "declined", error="You chose not to run this step.")
                        continue
                status = self._execute(con, run_id, position, step, cancel, (timeouts or {}).get(step.action),
                                       approved=frozenset(plan.picked))
            summary = summarize(plan, status)
            finish_run(con, run_id, status, summary)
            return status, summary
        finally:
            con.close()
            self._lock.release()

    def _set(self, con, run_id, position, step, status, result=None, error=None):
        step.status, step.result, step.error = status, result, error
        update_step(con, run_id, position, status, result, error)
        self._on_update(run_id, position, status, result or error or "")

    def _execute(self, con, run_id, position, step, cancel, timeout=None, approved=frozenset()):
        action = REGISTRY[step.action]
        self._set(con, run_id, position, step, "running")
        ctx = StepContext(self.services, cancel, approved=approved)
        box = {}

        def target():
            try:
                box["result"] = action.run(step.params, ctx)
            except ActionError as exc:
                box["error"] = str(exc)
            except PlanError as exc:
                box["error"] = str(exc)
            except Exception as exc:  # a bug or unexpected OS error: report, never crash
                box["error"] = f"Unexpected error: {type(exc).__name__}: {exc}"

        worker = threading.Thread(target=target, daemon=True, name=f"NovaTaskStep{position + 1}")
        worker.start()
        limit = timeout or action.timeout
        worker.join(limit)
        if worker.is_alive():
            # Ask the handler to stop. File actions stop between chunks and remove their
            # temporary file, so nothing is left half-done; give them a moment to report it.
            ctx.stop.set()
            worker.join(STOP_GRACE)
            if worker.is_alive():
                message = (f"Didn't finish within {limit:g} seconds. Nova asked it to stop and will ignore "
                           "its result.")
            elif "result" in box:
                message = (f"Took longer than {limit:g} seconds, so the task stopped here, although this step "
                           f"did finish: {box['result']}")
            else:
                message = f"Didn't finish within {limit:g} seconds and was stopped. {box.get('error', '')}".strip()
            self._set(con, run_id, position, step, "timed_out", error=message)
            return "timed_out"
        if "error" in box:
            self._set(con, run_id, position, step, "failed", error=box["error"])
            return "failed"
        self._set(con, run_id, position, step, "succeeded", result=box.get("result"))
        return "succeeded"  # a cancel during this step stops the task before the next one


def summarize(plan, status):
    done = sum(s.status == "succeeded" for s in plan.steps)
    heading = {
        "succeeded": (f"**Task complete**: all {len(plan.steps)} steps succeeded." if len(plan.steps) != 1
                      else "**Task complete**: the step succeeded."),
        "failed": f"**Task stopped**: a step failed, so the remaining steps weren't run ({done} of "
                  f"{len(plan.steps)} succeeded).",
        "timed_out": f"**Task stopped**: a step took too long ({done} of {len(plan.steps)} succeeded).",
        "cancelled": f"**Task cancelled** ({done} of {len(plan.steps)} steps had run).",
    }.get(status, f"**Task {status}**")
    lines = [heading]
    for number, step in enumerate(plan.steps, start=1):
        detail = step.result or step.error or ""
        lines.append(f"{number}. {STEP_ICONS.get(step.status, '·')} {step.description}"
                     + (f" — {detail}" if detail else ""))
    lines.append("\nDetails are in **Tasks**.")
    return "\n".join(lines)


import nova_actions  # noqa: E402,F401  (registers Step 25's file, clipboard and window actions)

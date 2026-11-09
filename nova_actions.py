"""
Expanded Windows computer interaction for Nova's task runner (Step 25). No GUI code.

Adds actions to nova_tasks.REGISTRY; nothing here replaces the Step 20 safety
model. Every action is a fixed Python handler with validated parameters, a
permission level, a timeout and a checked result.

  switch_app            bring an approved app's window to the front
  open_file             open a file the user chose or allowed, with its default app
                        (documents, images and media only: never programs or shortcuts)
  read_document         read a document into Nova's document index (Step 18), so it
                        can be asked about; only its name and size are recorded
  create_folder         create a folder in an allowed folder
  create_file           create a text file in an allowed folder (never overwrites)
  copy_file             copy one file to an allowed folder (replacing only if asked,
                        and only after confirming)
  move_file             move one file (always confirmed)
  rename_file           rename one file in its folder (always confirmed)
  recycle_file          move one file to the Recycle Bin (always confirmed; recoverable)
  copy_text_to_clipboard   put text on the clipboard
  save_clipboard_as_note   save the clipboard's text as a note (always confirmed;
                           refused if it looks like a password, key or token)

Paths
- Full local paths only: no network or device paths, no "." or ".." parts,
  no alternate data streams, wildcards, reserved names (CON, NUL, …), short
  8.3 names, or names ending in a dot or space.
- No part of a path may be a symbolic link, junction or mount point, so a
  path can't lead outside the folder it names (OneDrive placeholders are fine).
- Never inside Windows, Program Files, ProgramData, AppData (browser
  profiles, saved passwords, tokens and cookies live there), the Recycle
  Bin, key folders such as .ssh, or Nova's own folder; never key, password
  or credential files.
- Changing files needs the folder to be allowed: listed in Tasks › Allowed
  folders, chosen in a dialog, or allowed for one task when Nova asks.
- Everything is checked when the task is planned, again just before it runs,
  and again inside the handler right before the change.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import stat
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import nova_tasks as nt
from nova_tasks import (ASK, CHANGE, CLIPBOARD, DESTRUCTIVE, NONE, NOVA, READ, Action, ActionError, PlanError,
                        StepResult, _choice, _short, _text, approval_needed, register)

MAX_PATH_TEXT = 1024
MAX_FILE_NAME = 200
MAX_COPY_BYTES = 4 * 1024 ** 3          # 4 GB
MAX_CREATE_CHARS = 100_000
MAX_CLIPBOARD_CHARS = 20_000
COPY_CHUNK = 1024 * 1024

# Opened with their default app. Programs, scripts, shortcuts, installers, web pages and
# anything else that could run code are never opened.
OPENABLE = {
    ".txt", ".md", ".markdown", ".csv", ".log", ".json", ".pdf", ".rtf", ".docx", ".doc", ".xlsx", ".xls", ".pptx",
    ".ppt", ".odt", ".ods", ".odp", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".zip",
}
CREATABLE = {".txt", ".md", ".csv", ".json", ".log"}
# A file may never be renamed to (or created with) these: they run when opened.
RUNNABLE = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
    ".scr", ".pif", ".cpl", ".hta", ".lnk", ".url", ".reg", ".dll", ".sys", ".jar", ".py", ".pyw", ".appx",
    ".msix", ".application", ".gadget", ".inf", ".scf", ".chm", ".html", ".htm", ".svg", ".xml", ".xsl",
}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARS = set('<>"|?*')
PROTECTED_DIR_NAMES = {"$recycle.bin", "system volume information", "windowsapps", "appdata", ".ssh", ".aws",
                       ".azure", ".gnupg", ".kube", ".docker", ".git"}
PROTECTED_FILE_NAMES = {"ntuser.dat", ".env", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
                        "credentials", "cookies", "login data", "web data", "logins.json", "key3.db", "key4.db",
                        "cert9.db", "nova_memory.db", "nova_settings.json"}
PROTECTED_EXTENSIONS = {".kdbx", ".kdb", ".pem", ".key", ".pfx", ".p12", ".ppk", ".keychain", ".gpg", ".jks",
                        ".keystore", ".ovpn", ".rdp", ".cred"}


# ==================== path safety ====================
def parse_path(value, label="The path"):
    """A full local path, checked for traversal and unusual forms (not resolved yet)."""
    text = str(value or "").strip().strip("\"'“”").strip()
    if not text:
        raise PlanError(f"{label} is missing.")
    if len(text) > MAX_PATH_TEXT:
        raise PlanError(f"{label} is too long.")
    if any(ord(ch) < 32 for ch in text):
        raise PlanError(f"{label} contains control characters.")
    if text.startswith(("\\\\", "//")):
        raise PlanError("Network and device paths aren't allowed.")
    text = text.replace("/", "\\")
    if not re.match(r"^[A-Za-z]:\\", text):
        raise PlanError(f"{label} must be a full path such as C:\\Users\\you\\Documents\\file.txt, or let Nova "
                        "ask you to choose.")
    parts = [p for p in text[3:].split("\\") if p]
    for part in parts:
        if part in (".", ".."):
            raise PlanError(f"{label} can't contain “.” or “..” (that could point outside the folder).")
        if ":" in part:
            raise PlanError(f"{label} contains “:”, which isn't allowed after the drive letter.")
        if set(part) & _BAD_CHARS:
            raise PlanError(f"{label} contains characters that aren't allowed in Windows paths.")
        if part != part.rstrip(" ."):
            raise PlanError(f"“{_short(part)}” ends with a space or dot, which Windows doesn't allow reliably.")
        if part.split(".")[0].strip().lower() in _RESERVED:
            raise PlanError(f"“{_short(part)}” is a reserved Windows device name.")
        if re.search(r"~\d", part):
            raise PlanError("Use the full folder and file names, not short 8.3 names like PROGRA~1.")
    return Path(text[:3] + "\\".join(parts))


def is_link(path):
    """Symlinks, junctions and mount points (and app execution aliases).

    Other reparse points, such as OneDrive's online-only files, are ordinary
    files and folders for this purpose.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return True                      # can't inspect it: treat as unsafe
    if stat.S_ISLNK(info.st_mode):
        return True
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(info, "st_reparse_tag", 0)
        return bool(tag & 0x20000000) or tag == 0x8000001B   # name surrogates; AppExecLink
    return False


def no_links(path):
    """PlanError if any existing part of the path, from the drive down, is a link or junction."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            return
        if is_link(current):
            raise PlanError(f"{current} is a link or junction. Nova doesn't follow those, so a path can't lead "
                            "somewhere other than where it says.")


def _protected_roots(services):
    fn = getattr(services, "protected", None)
    return [os.path.normcase(str(p)).rstrip("\\/") for p in (fn() if fn else []) if p]


def check_not_protected(path, services):
    """PlanError for system, program and app-data folders, Nova's folder, and secret files."""
    text = os.path.normcase(str(path))
    for root in _protected_roots(services):
        if text == root or text.startswith(root + os.sep):
            raise PlanError(f"{path} is in a protected location (Windows, programs, app data, browser profiles or "
                            "Nova's own folder). Nova never changes or opens files there.")
    for part in Path(path).parts[1:]:
        if part.lower() in PROTECTED_DIR_NAMES:
            raise PlanError(f"{path} is inside “{part}”, which holds system data, keys or browser profiles. "
                            "Nova never touches it.")
    name = Path(path).name.lower()
    if (name in PROTECTED_FILE_NAMES or Path(name).suffix in PROTECTED_EXTENSIONS or name.startswith("id_rsa")
            or name.startswith("id_ed25519") or name.startswith("id_ecdsa")):
        raise PlanError(f"{Path(path).name} looks like a password, key, credential or Nova data file. "
                        "Nova never touches those.")


def _resolve(path, label):
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise PlanError(f"{label} {path} doesn't exist.") from None
    if str(resolved).startswith("\\\\"):
        raise PlanError("Network and device paths aren't allowed.")
    return resolved


def checked_file(value, services, label="The file"):
    path = parse_path(value, label)
    no_links(path)
    resolved = _resolve(path, label)
    if resolved.is_dir():
        raise PlanError(f"{resolved} is a folder. File actions work on single files, never whole folders.")
    if not resolved.is_file():
        raise PlanError(f"{resolved} isn't a regular file.")
    check_not_protected(resolved, services)
    return resolved


def checked_folder(value, services, label="The folder"):
    path = parse_path(value, label)
    no_links(path)
    resolved = _resolve(path, label)
    if not resolved.is_dir():
        raise PlanError(f"{resolved} is a file, not a folder.")
    check_not_protected(resolved, services)
    return resolved


def check_name(value, label="The name", allowed=None):
    """A plain file or folder name: no folders, drive letters or tricks."""
    name = _text(value, label, MAX_FILE_NAME).strip("\"'“”")
    if not name or "\\" in name or "/" in name or ":" in name or set(name) & _BAD_CHARS:
        raise PlanError(f"{label} must be a plain name without folders, drive letters or the characters "
                        "\\ / : * ? \" < > |.")
    if name in (".", "..") or name != name.rstrip(" ."):
        raise PlanError(f"{label} can't be “.”, “..” or end with a space or dot.")
    if name.split(".")[0].strip().lower() in _RESERVED:
        raise PlanError(f"“{name}” is a reserved Windows device name.")
    suffix = Path(name).suffix.lower()
    if suffix in RUNNABLE or suffix in PROTECTED_EXTENSIONS:
        raise PlanError(f"Nova doesn't create files ending in {suffix}: they can run code or hold keys.")
    if allowed is not None and suffix not in allowed:
        raise PlanError(f"New files can be {', '.join(sorted(allowed))} (text only).")
    return name


def _file_param(value, services, allow_ask, label="The file"):
    if str(value or "").strip().lower() == ASK:
        if not allow_ask:
            raise PlanError("Choose the file before running this step.")
        return ASK
    return str(checked_file(value, services, label))


def _folder_param(value, services, allow_ask, label="The folder"):
    if str(value or "").strip().lower() == ASK:
        if not allow_ask:
            raise PlanError("Choose the folder before running this step.")
        return ASK
    key = str(value or "").strip().lower()
    if key in nt.KNOWN_FOLDERS:
        return str(nt.resolve_folder(key, services))
    return str(checked_folder(value, services, label))


def _bool(value, label):
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0"):
        return False
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no"):
        return False
    raise PlanError(f"{label} must be true or false.")


def _paths(*names_and_needs):
    def collect(params):
        return [(params[name], need) for name, need in names_and_needs
                if params.get(name) not in (None, ASK)]
    return collect


# ==================== run-time re-checks ====================
def recheck_file(path, ctx, need):
    """Everything again, right before the change (the file or folders may have changed since planning)."""
    try:
        resolved = checked_file(path, ctx.services)
    except PlanError as exc:
        raise ActionError(str(exc)) from None
    _require_allowed(resolved, ctx, need)
    return resolved


def recheck_folder(path, ctx, need):
    try:
        resolved = checked_folder(path, ctx.services)
    except PlanError as exc:
        raise ActionError(str(exc)) from None
    _require_allowed(resolved, ctx, need)
    return resolved


def _require_allowed(path, ctx, need):
    root = approval_needed(str(path), need, ctx.services, ctx.approved)
    if root:
        raise ActionError(f"Nova isn't allowed to {'change' if need == 'change' else 'open'} files in {root}. "
                          "Allow the folder in Tasks › Allowed folders, or choose it when Nova asks.")


def _replaceable(target):
    """The existing file a confirmed replace may overwrite: a plain file with one name."""
    if is_link(target) or not os.path.isfile(target):
        raise ActionError(f"{target} isn't a plain file, so Nova won't replace it.")
    if os.stat(target).st_nlink > 1:
        raise ActionError(f"{target} has other names elsewhere (a hard link), so replacing it could change "
                          "files outside the folder. Nova won't replace it.")


def _copy_contents(source, folder, name, ctx, overwrite):
    """Copy through a temporary file in the destination, then rename it into place.

    Stops between chunks when the task is cancelled or times out, and always
    removes the temporary file, so a stopped copy leaves nothing behind. The
    final rename never replaces an existing file unless the user confirmed it.
    """
    target = Path(folder) / name
    temp = Path(folder) / f".nova-copy-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with open(source, "rb") as reader, open(temp, "xb") as writer:
            while True:
                if ctx.stopped():
                    raise ActionError("Stopped before the copy finished. Nothing was changed.")
                chunk = reader.read(COPY_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        try:
            shutil.copystat(source, temp)
        except OSError:
            pass
        if os.path.getsize(temp) != os.path.getsize(source):
            raise ActionError("The copy didn't match the original. Nothing was changed.")
        if ctx.stopped():
            raise ActionError("Stopped before the copy finished. Nothing was changed.")
        if overwrite and os.path.lexists(target):
            _replaceable(target)
            os.replace(temp, target)
        else:
            os.rename(temp, target)      # fails if the file appeared in the meantime: never overwrites
        return target
    except FileExistsError:
        raise ActionError(f"{target} already exists, so it wasn't replaced.") from None
    except PermissionError as exc:
        raise ActionError(f"Windows didn't allow this ({exc.strerror or exc}). Nova doesn't bypass "
                          "permissions.") from None
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


# ==================== validators ====================
SWITCH_APPS = {
    "notepad": ("Notepad", {"notepad.exe"}),
    "calculator": ("Calculator", {"calculatorapp.exe", "calculator.exe", "calc.exe"}),
    "paint": ("Paint", {"mspaint.exe", "paintapp.exe"}),
    "vscode": ("Visual Studio Code", {"code.exe"}),
    "explorer": ("File Explorer", {"explorer.exe"}),
    "browser": ("your web browser", {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe",
                                     "vivaldi.exe"}),
    "nova": ("Nova", set()),
}
SWITCH_ALIASES = {**nt.APP_ALIASES, "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
                  "browser": "browser", "web browser": "browser", "edge": "browser", "chrome": "browser",
                  "firefox": "browser", "microsoft edge": "browser", "google chrome": "browser"}


def _v_switch(raw, services, allow_ask):
    return {"app": _choice(raw.get("app"), "app to switch to", [k for k in SWITCH_APPS if k != "nova"],
                           SWITCH_ALIASES)}


def _v_open_file(raw, services, allow_ask):
    file = _file_param(raw.get("file"), services, allow_ask)
    if file != ASK and Path(file).suffix.lower() not in OPENABLE:
        kind = Path(file).suffix or "files without an extension"
        raise PlanError(f"Nova only opens documents, pictures and media, not {kind}: other files can run programs.")
    return {"file": file}


def _v_read(raw, services, allow_ask):
    import nova_documents
    file = _file_param(raw.get("file"), services, allow_ask, "The document")
    if file != ASK and Path(file).suffix.lower() not in nova_documents.SUPPORTED_TYPES:
        raise PlanError(f"Nova can read {', '.join(sorted(nova_documents.SUPPORTED_TYPES))} documents.")
    return {"file": file}


def _v_create_folder(raw, services, allow_ask):
    folder = _folder_param(raw.get("folder"), services, allow_ask, "The folder to create it in")
    name = check_name(raw.get("name"), "The new folder's name")
    if folder != ASK and os.path.lexists(Path(folder) / name):
        raise PlanError(f"{Path(folder) / name} already exists.")
    return {"folder": folder, "name": name}


def _v_create_file(raw, services, allow_ask):
    folder = _folder_param(raw.get("folder"), services, allow_ask, "The folder to create it in")
    name = str(raw.get("name") or "").strip().strip("\"'“”")
    if name and not Path(name).suffix:
        name += ".txt"
    name = check_name(name, "The new file's name", CREATABLE)
    content = _text(raw.get("content"), "The file's text", MAX_CREATE_CHARS, required=False, multiline=True)
    if folder != ASK and os.path.lexists(Path(folder) / name):
        raise PlanError(f"{Path(folder) / name} already exists. Nova never overwrites a file it creates.")
    return {"folder": folder, "name": name, "content": content}


def _v_transfer(raw, services, allow_ask):
    file = _file_param(raw.get("file"), services, allow_ask)
    destination = _folder_param(raw.get("destination"), services, allow_ask, "The destination folder")
    overwrite = _bool(raw.get("overwrite"), "“Replace an existing file”")
    if file != ASK and Path(file).suffix.lower() in RUNNABLE:
        raise PlanError(f"Nova doesn't copy or move {Path(file).suffix} files: they can run code.")
    if file != ASK and destination != ASK:
        source = Path(file)
        if os.path.normcase(str(source.parent)) == os.path.normcase(destination):
            raise PlanError(f"{source.name} is already in {destination}.")
        if os.path.getsize(source) > MAX_COPY_BYTES:
            raise PlanError(f"{source.name} is larger than {MAX_COPY_BYTES // 1024 ** 3} GB.")
        target = Path(destination) / source.name
        if os.path.lexists(target):
            if not overwrite:
                raise PlanError(f"{target} already exists. Say “replacing the existing file” if you want to "
                                "replace it (Nova will ask you to confirm).")
            if is_link(target) or not target.is_file():
                raise PlanError(f"{target} isn't a plain file, so Nova won't replace it.")
            check_not_protected(target, services)
    return {"file": file, "destination": destination, "overwrite": overwrite}


def _v_rename(raw, services, allow_ask):
    file = _file_param(raw.get("file"), services, allow_ask)
    name = check_name(raw.get("name"), "The new name")
    if file != ASK:
        source = Path(file)
        if not Path(name).suffix and source.suffix:
            name += source.suffix          # "rename it to report" keeps .docx
            name = check_name(name, "The new name")
        target = source.parent / name
        if os.path.normcase(str(target)) != os.path.normcase(str(source)) and os.path.lexists(target):
            raise PlanError(f"{target} already exists. Nova doesn't overwrite files when renaming.")
        if target == source:
            raise PlanError("The file already has that name.")
        check_not_protected(target, services)
    return {"file": file, "name": name}


def _v_recycle(raw, services, allow_ask):
    return {"file": _file_param(raw.get("file"), services, allow_ask)}


def _v_copy_text(raw, services, allow_ask):
    return {"text": _text(raw.get("text"), "The text to copy", MAX_CLIPBOARD_CHARS, multiline=True)}


def _v_clipboard_note(raw, services, allow_ask):
    title = _text(raw.get("title"), "The note title", nt.MAX_NOTE_TITLE, required=False) or "From the clipboard"
    return {"title": title}


# ==================== confirmations (decided here, never by a model) ====================
def _s_transfer(params, services, verb):
    target = None
    if params["file"] != ASK and params["destination"] != ASK:
        target = Path(params["destination"]) / Path(params["file"]).name
    replaces = bool(params.get("overwrite") and target is not None and os.path.lexists(target))
    if verb == "move":
        return "moves a file out of its folder" + (", replacing a file with the same name" if replaces else "")
    if params.get("overwrite"):
        return "replaces an existing file with the same name" if replaces or target is None else \
            "may replace a file with the same name"
    return None


# ==================== handlers (each re-checks, then verifies its result) ====================
def _r_switch(params, ctx):
    title, _exes = SWITCH_APPS[params["app"]]
    if ctx.services.switch_window is None:
        raise ActionError("Switching windows isn't available.")
    found = ctx.services.switch_window(params["app"])
    if found is None:
        raise ActionError(f"{title} isn't open. Ask Nova to open it first.")
    if not found:
        raise ActionError(f"Windows didn't let Nova bring {title} to the front (its taskbar button may be "
                          "flashing instead).")
    return StepResult(f"Switched to {title}.", {"app": params["app"]})


def _r_open_file(params, ctx):
    path = recheck_file(params["file"], ctx, "read")
    if path.suffix.lower() not in OPENABLE:
        raise ActionError("That kind of file isn't opened by Nova.")
    try:
        (ctx.services.open_file or os.startfile)(str(path))
    except OSError as exc:
        raise ActionError(f"Windows couldn't open {path.name}: {exc.strerror or exc}") from None
    return StepResult(f"Opened {path} with its default app.", {"file": str(path)})


def _r_read(params, ctx):
    path = recheck_file(params["file"], ctx, "read")
    if ctx.services.index_document is None:
        raise ActionError("Reading documents isn't available.")
    status, passages, detail = ctx.services.index_document(str(path), ctx.stopped)
    if ctx.stopped():
        raise ActionError("Stopped before the document was read.")
    if status not in ("indexed", "duplicate"):
        raise ActionError(f"Nova couldn't read {path.name}: {detail or status}.")
    size = os.path.getsize(path)
    extra = " (identical to a document already indexed)" if status == "duplicate" else ""
    return StepResult(f"Read {path.name} ({size / 1024:,.0f} KB, {passages} passage{'s' if passages != 1 else ''})"
                      f"{extra}. Ask about it with “docs: …”.",
                      {"file": str(path), "passages": passages, "status": status})


def _r_create_folder(params, ctx):
    parent = recheck_folder(params["folder"], ctx, "change")
    target = parent / check_name(params["name"])
    try:
        os.mkdir(target)
    except FileExistsError:
        raise ActionError(f"{target} already exists.") from None
    except PermissionError as exc:
        raise ActionError(f"Windows didn't allow creating it ({exc.strerror or exc}).") from None
    if not target.is_dir() or is_link(target):
        raise ActionError("The folder couldn't be confirmed after creating it.")
    return StepResult(f"Created the folder {target}.", {"folder": str(target)})


def _r_create_file(params, ctx):
    parent = recheck_folder(params["folder"], ctx, "change")
    target = parent / check_name(params["name"], allowed=CREATABLE)
    try:
        with open(target, "x", encoding="utf-8", newline="") as handle:   # "x": never overwrites
            handle.write(params["content"])
    except FileExistsError:
        raise ActionError(f"{target} already exists, so Nova didn't overwrite it.") from None
    except PermissionError as exc:
        raise ActionError(f"Windows didn't allow creating it ({exc.strerror or exc}).") from None
    size = os.path.getsize(target)
    return StepResult(f"Created {target} ({size:,} bytes).", {"file": str(target), "bytes": size})


def _r_copy(params, ctx):
    source = recheck_file(params["file"], ctx, "read")
    folder = recheck_folder(params["destination"], ctx, "change")
    target = _copy_contents(source, folder, source.name, ctx, params["overwrite"])
    return StepResult(f"Copied {source} to {target}.", {"source": str(source), "file": str(target)})


def _r_move(params, ctx):
    source = recheck_file(params["file"], ctx, "change")
    folder = recheck_folder(params["destination"], ctx, "change")
    target = folder / source.name
    if os.path.splitdrive(str(source))[0].lower() == os.path.splitdrive(str(folder))[0].lower():
        try:
            if params["overwrite"] and os.path.lexists(target):
                _replaceable(target)
                os.replace(source, target)
            else:
                os.rename(source, target)       # never overwrites
        except FileExistsError:
            raise ActionError(f"{target} already exists, so the file wasn't moved.") from None
        except PermissionError as exc:
            raise ActionError(f"Windows didn't allow moving it ({exc.strerror or exc}).") from None
    else:                                        # another drive: copy, check, then remove the original
        _copy_contents(source, folder, source.name, ctx, params["overwrite"])
        try:
            os.remove(source)
        except OSError as exc:
            raise ActionError(f"Copied to {target}, but the original couldn't be removed ({exc}). "
                              "Both copies exist.") from None
    if not target.is_file() or source.exists():
        raise ActionError("The move couldn't be confirmed.")
    return StepResult(f"Moved {source} to {target}.", {"source": str(source), "file": str(target)})


def _r_rename(params, ctx):
    source = recheck_file(params["file"], ctx, "change")
    target = source.parent / check_name(params["name"])
    if os.path.normcase(str(target)) != os.path.normcase(str(source)) and os.path.lexists(target):
        raise ActionError(f"{target} already exists, so the file wasn't renamed.")
    try:
        os.rename(source, target)
    except FileExistsError:
        raise ActionError(f"{target} already exists, so the file wasn't renamed.") from None
    except PermissionError as exc:
        raise ActionError(f"Windows didn't allow renaming it ({exc.strerror or exc}).") from None
    if not target.is_file():
        raise ActionError("The rename couldn't be confirmed.")
    return StepResult(f"Renamed {source.name} to {target.name} in {source.parent}.",
                      {"source": str(source), "file": str(target)})


def _r_recycle(params, ctx):
    path = recheck_file(params["file"], ctx, "change")
    if ctx.services.recycle is None:
        raise ActionError("The Recycle Bin isn't available.")
    try:
        ctx.services.recycle(str(path))
    except OSError as exc:
        raise ActionError(f"Windows couldn't move it to the Recycle Bin ({exc}).") from None
    if os.path.lexists(path):
        raise ActionError(f"{path.name} is still there: Windows didn't move it to the Recycle Bin.")
    return StepResult(f"Moved {path} to the Recycle Bin (you can restore it from there).", {"file": str(path)})


def _r_copy_text(params, ctx):
    if ctx.services.clipboard_set is None:
        raise ActionError("The clipboard isn't available.")
    ctx.services.clipboard_set(params["text"])
    if ctx.services.clipboard_get and ctx.services.clipboard_get() != params["text"]:
        raise ActionError("The text couldn't be confirmed on the clipboard.")
    return StepResult(f"Copied {len(params['text']):,} characters to the clipboard.",
                      {"characters": len(params["text"])})


def _r_clipboard_note(params, ctx):
    if ctx.services.clipboard_get is None:
        raise ActionError("The clipboard isn't available.")
    text = ctx.services.clipboard_get() or ""
    if not text.strip():
        raise ActionError("There's no text on the clipboard.")
    if looks_secret(text):
        raise ActionError("The clipboard seems to hold a password, key or token, so Nova didn't read it into a "
                          "note.")
    if len(text) > nt.MAX_NOTE_CONTENT:
        raise ActionError(f"The clipboard text is longer than {nt.MAX_NOTE_CONTENT:,} characters, the most a note "
                          "can hold.")
    note_id = ctx.services.save_note(params["title"], text.strip())
    saved = ctx.services.get_note(note_id)
    if not saved or saved[1] != params["title"]:
        raise ActionError("The note couldn't be confirmed in the database.")
    return StepResult(f"Saved {len(text.strip()):,} characters from the clipboard as note #{note_id}: "
                      f"{params['title']}.", {"note_id": note_id})


_SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|gh[ousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b"
    r"|(?i:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,})"
    r"|(?i:\b(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|cookie)"
    r"\s*[:=])"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"     # JSON web tokens
    r"|(?i:\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@)")                    # credentials in a URL


def looks_secret(text):
    """True for text that looks like a password, key, token or cookie (heuristic)."""
    text = str(text or "")
    if _SECRET.search(text):
        return True
    single = text.strip()
    # One "word" of 10–128 characters mixing cases, digits and symbols: probably a password or key.
    # (Paths, web addresses and email addresses are ordinary text.)
    if (single and not re.search(r"\s", single) and 10 <= len(single) <= 128
            and not re.match(r"^(?:[A-Za-z]:\\|https?://|www\.)", single)
            and not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", single)):
        classes = sum(bool(re.search(p, single)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
        if classes >= 3:
            return True
    return False


# ==================== registration ====================
def _preview(text, limit=300):
    text = str(text or "")
    return (text[:limit] + "…") if len(text) > limit else (text or None)


def _label(value, kind="file"):
    if value == ASK:
        return "a file you choose" if kind == "file" else "a folder you choose"
    return str(value)


ACTIONS = (
    Action("switch_app", "Switch to an open app",
           "switch_app(app: notepad|calculator|paint|vscode|explorer|browser)",
           frozenset({"app"}), 5, _v_switch, lambda p: f"Switch to {SWITCH_APPS[p['app']][0]}",
           nt._never, _r_switch, permission=NONE, alone=True),
    Action("open_file", "Open a file", "open_file(file: ask|<full file path the user gave>)",
           frozenset({"file"}), 10, _v_open_file, lambda p: f"Open {_label(p['file'])} with its default app",
           nt._never, _r_open_file, permission=READ, paths=_paths(("file", "read")), alone=True),
    Action("read_document", "Read a document into Nova's documents",
           "read_document(file: ask|<full path of a .txt, .md, .pdf, .docx, .csv or .xlsx file>)",
           frozenset({"file"}), 180, _v_read,
           lambda p: f"Read {_label(p['file'])} into your documents, so you can ask about it",
           nt._never, _r_read, permission=READ, paths=_paths(("file", "read")), alone=True),
    Action("create_folder", "Create a folder", "create_folder(folder: ask|<full folder path>, name: new folder name)",
           frozenset({"folder", "name"}), 10, _v_create_folder,
           lambda p: f"Create the folder “{p['name']}” in {_label(p['folder'], 'folder')}",
           nt._never, _r_create_folder, permission=CHANGE, paths=_paths(("folder", "change")), alone=True),
    Action("create_file", "Create a text file",
           "create_file(folder: ask|<full folder path>, name: new file name (.txt, .md, .csv, .json), content: text)",
           frozenset({"folder", "name", "content"}), 10, _v_create_file,
           lambda p: (f"Create the text file “{p['name']}” in {_label(p['folder'], 'folder')}"
                      + (f" ({len(p['content']):,} characters)" if p["content"] else " (empty)")),
           nt._never, _r_create_file, permission=CHANGE, private=frozenset({"content"}),
           paths=_paths(("folder", "change")), alone=True, preview=lambda p: _preview(p["content"])),
    Action("copy_file", "Copy a file",
           "copy_file(file: ask|<full file path>, destination: ask|<full folder path>, overwrite: false)",
           frozenset({"file", "destination", "overwrite"}), 300, _v_transfer,
           lambda p: (f"Copy {_label(p['file'])} to {_label(p['destination'], 'folder')}"
                      + (" (replacing a file with the same name)" if p["overwrite"] else "")),
           lambda p, s: _s_transfer(p, s, "copy"), _r_copy, permission=CHANGE,
           paths=_paths(("file", "read"), ("destination", "change")), alone=True),
    Action("move_file", "Move a file",
           "move_file(file: ask|<full file path>, destination: ask|<full folder path>, overwrite: false)",
           frozenset({"file", "destination", "overwrite"}), 300, _v_transfer,
           lambda p: (f"Move {_label(p['file'])} to {_label(p['destination'], 'folder')}"
                      + (" (replacing a file with the same name)" if p["overwrite"] else "")),
           lambda p, s: _s_transfer(p, s, "move"), _r_move, permission=DESTRUCTIVE,
           paths=_paths(("file", "change"), ("destination", "change")), alone=True),
    Action("rename_file", "Rename a file", "rename_file(file: ask|<full file path>, name: new file name)",
           frozenset({"file", "name"}), 10, _v_rename,
           lambda p: f"Rename {_label(p['file'])} to “{p['name']}”",
           lambda p, s: "renames a file", _r_rename, permission=DESTRUCTIVE,
           paths=_paths(("file", "change")), alone=True),
    Action("recycle_file", "Move a file to the Recycle Bin", "recycle_file(file: ask|<full file path>)",
           frozenset({"file"}), 30, _v_recycle,
           lambda p: f"Move {_label(p['file'])} to the Recycle Bin",
           lambda p, s: "moves a file to the Recycle Bin (you can restore it from there)", _r_recycle,
           permission=DESTRUCTIVE, paths=_paths(("file", "change")), alone=True),
    Action("copy_text_to_clipboard", "Copy text to the clipboard", "copy_text_to_clipboard(text: text)",
           frozenset({"text"}), 5, _v_copy_text,
           lambda p: f"Copy {len(p['text']):,} characters of text to the clipboard",
           lambda p, s: "replaces what's on your clipboard", _r_copy_text, permission=CLIPBOARD,
           private=frozenset({"text"}), alone=True, preview=lambda p: _preview(p["text"])),
    Action("save_clipboard_as_note", "Save the clipboard as a note", "save_clipboard_as_note(title: text)",
           frozenset({"title"}), 5, _v_clipboard_note,
           lambda p: f"Save the text on your clipboard as the note “{p['title']}”",
           lambda p, s: "reads what's on your clipboard (refused if it looks like a password or key)",
           _r_clipboard_note, permission=CLIPBOARD, alone=True),
)


# ==================== rules for the deterministic planner ====================
_PATH = r"(?:\"[^\"]+\"|“[^”]+”|[A-Za-z]:\\[^\"\n]*?)"


def _file_expr(phrase, context):
    """A file phrase: a path, “a file I choose”, or “it” (the file from an earlier step)."""
    text = phrase.strip().rstrip(".!").strip()
    low = re.sub(r"^(?:the|my|this|that)\s+(?:file|document)\s+", "", text.lower()).strip()
    if low in ("it", "that", "this", "that file", "this file", "the file", "the same file", "the copy"):
        if context.get("file"):
            return context["file"]
        raise PlanError("It isn't clear which file “it” refers to.")
    if re.fullmatch(r"(?:an?\s+|one\s+|the\s+)?(?:file|document|pdf)(?:\s+(?:i|you)\s+(?:choose|select|pick)"
                    r"|\s+of\s+my\s+choice)?", low):
        return ASK
    path = re.sub(r"^(?:the\s+|my\s+)?(?:file|document)\s+", "", text, flags=re.IGNORECASE).strip()
    if re.fullmatch(_PATH, path) or re.match(r"^[A-Za-z]:\\", path):
        return path.strip("\"“”")
    return None


def _dest_expr(phrase, context):
    folder = nt._folder_expr(phrase, context)
    if folder is None:
        raise PlanError(f"“{_short(phrase)}” isn't a folder Nova can use. Give its full path, or say "
                        "“a folder I choose”.")
    return folder


def parse_clause(text, context):
    """Clauses for Step 25's actions, or None if the clause isn't one of them."""
    t = text.strip().rstrip(".!").strip()
    low = t.lower()

    m = re.fullmatch(r"(?:switch|change|go\s+back)\s+to\s+(?:the\s+|my\s+)?(?P<app>.+?)(?:\s+(?:window|app))?"
                     r"|(?:bring\s+up|focus(?:\s+on)?)\s+(?:the\s+|my\s+)?(?P<app2>.+?)(?:\s+(?:window|app))?", low)
    if m:
        app = (m.group("app") or m.group("app2")).strip()
        key = SWITCH_ALIASES.get(app)
        if key is None or key == "nova":
            raise PlanError(f"Nova can switch to Notepad, Calculator, Paint, VS Code, File Explorer or your "
                            f"browser, not “{_short(app)}”.")
        return [("switch_app", {"app": key})]

    m = re.fullmatch(r"copy\s+(?P<text>.+?)\s+to\s+(?:the\s+|my\s+)?clipboard", t, re.I | re.S)
    if m:
        return [("copy_text_to_clipboard", {"text": m.group("text").strip().strip("\"'“”")})]
    m = re.fullmatch(r"(?:save|turn|make|put)\s+(?:what(?:'s|\s+is)\s+(?:on|in)\s+)?(?:my\s+|the\s+)?clipboard"
                     r"\s+(?:into|as|in|to)\s+(?:a\s+)?(?:new\s+)?note(?:\s+(?:called|titled|named)\s+(?P<title>.+))?",
                     t, re.I)
    if m:
        return [("save_clipboard_as_note", {"title": (m.group("title") or "From the clipboard").strip("\"'“”")})]

    m = (re.fullmatch(r"move\s+(?P<file>.+?)\s+to\s+(?:the\s+)?(?:recycle\s+bin|trash|bin)", t, re.I)
         or re.fullmatch(r"(?:delete|remove|recycle|trash)\s+(?P<file>.+)", t, re.I))
    if m:
        file = _file_expr(m.group("file"), context)
        if file is None:
            raise PlanError(f"“{_short(m.group('file'))}” isn't a single file. Nova only recycles one file at a "
                            "time: give its full path or say “a file I choose”.")
        return [("recycle_file", {"file": file})]

    m = re.fullmatch(r"(?P<verb>copy|move)\s+(?P<file>.+?)\s+(?:to|into)\s+(?P<dest>.+?)"
                     r"(?P<over>,?\s*(?:and\s+)?(?:replac|overwrit)\w*(?:\s+(?:the\s+|any\s+)?(?:existing|old)"
                     r"(?:\s+(?:one|file|copy|version))?)?)?", t, re.I)
    if m:
        file = _file_expr(m.group("file"), context)
        if file is None:
            return None
        dest = _dest_expr(m.group("dest"), context)
        if file != ASK:
            context["file"] = str(Path(dest) / Path(file).name) if dest not in (ASK, *nt.KNOWN_FOLDERS) else None
        return [(f"{m.group('verb').lower()}_file", {"file": file, "destination": dest,
                                                      "overwrite": bool(m.group("over"))})]

    m = re.fullmatch(r"rename\s+(?P<file>.+?)\s+(?:to|as)\s+(?P<name>.+)", t, re.I)
    if m:
        file = _file_expr(m.group("file"), context)
        if file is None:
            raise PlanError(f"“{_short(m.group('file'))}” isn't a file Nova can rename: give its full path or say "
                            "“a file I choose”.")
        name = m.group("name").strip().strip("\"'“”")
        if file != ASK:
            context["file"] = str(Path(file).parent / name)
        return [("rename_file", {"file": file, "name": name})]

    m = re.fullmatch(r"(?:create|make|add|write)\s+(?:a\s+)?(?:new\s+)?(?:text\s+)?file\s+(?:called|named|titled)\s+"
                     r"(?P<name>.+?)\s+in\s+(?P<folder>.+?)(?:\s+(?:saying|that\s+says|with\s+the\s+text|containing)"
                     r"\s+(?P<content>.+))?", t, re.I | re.S)
    if m:
        folder_text, content = m.group("folder"), m.group("content") or ""
        # "in C:\Work with eggs and milk": an existing folder followed by "with …" — the rest is the text.
        split = re.match(r"^(?P<path>[A-Za-z]:\\.+?)\s+(?:with|listing|including)\s+(?P<rest>.+)$", folder_text, re.I | re.S)
        if not content and split and os.path.isdir(split.group("path")) and not os.path.isdir(folder_text):
            folder_text, content = split.group("path"), split.group("rest")
        folder = _dest_expr(folder_text, context)
        name = m.group("name").strip().strip("\"'“”")
        if folder not in (ASK, *nt.KNOWN_FOLDERS):
            context["file"] = str(Path(folder) / (name if Path(name).suffix else name + ".txt"))
        return [("create_file", {"folder": folder, "name": name,
                                 "content": content.strip().strip("\"“”")})]
    m = re.fullmatch(r"(?:create|make|add)\s+(?:a\s+)?(?:new\s+)?folder\s+(?:called|named)\s+(?P<name>.+?)\s+in\s+"
                     r"(?P<folder>.+)", t, re.I)
    if m:
        folder = _dest_expr(m.group("folder"), context)
        name = m.group("name").strip().strip("\"'“”")
        if folder not in (ASK, *nt.KNOWN_FOLDERS):
            context["folder"] = str(Path(folder) / name)
        return [("create_folder", {"folder": folder, "name": name})]

    m = (re.fullmatch(r"(?:read|index|import)\s+(?P<file>.+?)(?:\s+(?:in)?to\s+(?:my\s+)?documents)?", t, re.I)
         or re.fullmatch(r"add\s+(?P<file>.+?)\s+to\s+(?:my\s+)?documents", t, re.I))
    if m:
        file = _file_expr(m.group("file"), context)
        if file is not None:
            return [("read_document", {"file": file})]
        return None

    m = re.fullmatch(r"open\s+(?P<file>.+)", t, re.I)
    if m:
        phrase = m.group("file")
        explicit = re.match(r"(?:the\s+|a\s+|my\s+)?(?:file|document)\b", phrase.strip(), re.I)
        file = _file_expr(phrase, context)
        if file is None:
            return None
        if file == ASK or explicit or (Path(file).suffix and not os.path.isdir(file)):
            context["file"] = file if file != ASK else None
            return [("open_file", {"file": file})]
    return None


# ==================== Windows services (the real ones; tests use fakes) ====================
_KNOWN_FOLDER_IDS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def known_folder(name):
    """Where Windows keeps the user's Desktop, Documents, Downloads or Pictures (OneDrive-aware)."""
    fallback = Path.home() / name.title()
    try:
        raw = uuid.UUID(_KNOWN_FOLDER_IDS[name]).bytes_le
        guid = _GUID.from_buffer_copy(raw)
        shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
            return fallback
        try:
            return Path(out.value)
        finally:
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
    except Exception:
        return fallback


def default_protected(app_folder=None):
    env, home = os.environ, Path.home()
    roots = [env.get("SystemRoot") or r"C:\Windows", env.get("ProgramFiles"), env.get("ProgramFiles(x86)"),
             env.get("ProgramW6432"), env.get("ProgramData"), env.get("APPDATA"), env.get("LOCALAPPDATA"),
             str(home / "AppData"), str(app_folder or Path(__file__).resolve().parent)]
    return [r for r in roots if r]


# ---- Recycle Bin ----
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def recycle(path):
    """Move one file to the Recycle Bin. If Windows can only delete it permanently (no Recycle
    Bin on that drive), Windows itself warns and asks first."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO = 3, 0x4, 0x10, 0x40
    FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x400, 0x4000
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(path) + "\0"          # the structure needs a double-null-terminated list
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING | FOF_SILENT | FOF_NOERRORUI
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    result = shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"the operation was cancelled or failed (code {result})")


# ---- clipboard ----
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002


def _clipboard_api():
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return user32, kernel32


def _open_clipboard(user32):
    for _ in range(10):                  # another app may hold it for a moment
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("the clipboard is in use by another app")


def clipboard_get():
    user32, kernel32 = _clipboard_api()
    _open_clipboard(user32)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set(text):
    user32, kernel32 = _clipboard_api()
    data = ctypes.create_unicode_buffer(str(text))
    size = ctypes.sizeof(data)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise OSError("not enough memory for the clipboard")
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, size)
    kernel32.GlobalUnlock(handle)
    _open_clipboard(user32)
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError("Windows didn't accept the clipboard text")
    finally:
        user32.CloseClipboard()


# ---- switching windows ----
def _process_name(pid):
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""                                            # e.g. an elevated app: not ours to touch
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def app_windows(app):
    """Top-level windows of an approved app: [(hwnd, title)]. Titles are only used to pick a window."""
    title_text, exes = SWITCH_APPS[app]
    user32 = ctypes.WinDLL("user32")
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [proc, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):     # GW_OWNER: skip dialogs
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_name(pid.value)
        match = exe in exes
        if app == "explorer":
            match = exe == "explorer.exe" and cls.value == "CabinetWClass"
        elif exe == "applicationframehost.exe":               # packaged apps such as Calculator
            match = title_text.lower() in title.value.lower()
        if match:
            found.append((hwnd, title.value))
        return True

    user32.EnumWindows(proc(visit), 0)
    return found


def switch_window(app):
    """Bring the first window of an approved app to the front. None if it isn't open,
    False if Windows refused (Nova never works around that)."""
    windows = app_windows(app)
    if not windows:
        return None
    hwnd = windows[0][0]
    user32 = ctypes.WinDLL("user32")
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                            # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


# ==================== register ====================
for _action in ACTIONS:
    if _action.name not in nt.REGISTRY:
        register(_action)
if parse_clause not in nt.CLAUSE_PARSERS:
    nt.CLAUSE_PARSERS.append(parse_clause)

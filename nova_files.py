"""
File management tools for Nova's agent (Step 30). No GUI code.

  list_folder       list a folder's files and subfolders (read-only)
  read_text_file    read a text file's contents for the agent (read-only; contents never stored)
  organize_files    sort a folder's files into subfolders by type, extension or month:
                    previewed, always confirmed, every move recorded in the audit trail

Searching, opening, reading into Documents, creating, copying, moving,
renaming and recycling single files are the Step 25 actions, reused as they are.

Safety: every path goes through nova_actions' checks (full local paths only, no
“..”, links, junctions, network paths, protected folders or key/credential
files). Reading needs a folder the user allowed or one of Downloads,
Documents, Desktop and Pictures; changing needs an allowed folder (Nova asks
otherwise). organize_files moves exactly the files shown in its preview, never
replaces a file, never recurses into subfolders, never moves programs or
scripts, re-checks each file right before moving it, and stops at once when
the task is stopped (what was moved is reported and audited).
"""
from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path

import nova_actions
import nova_agent as na
import nova_tasks as nt

MAX_LIST = 200
MAX_ORGANIZE = 200
MAX_READ_BYTES = 2 * 1024 * 1024
DEFAULT_READ_CHARS, MAX_READ_CHARS = 4000, 20000
READABLE = {".txt", ".md", ".markdown", ".csv", ".log", ".json", ".tsv"}
CATEGORIES = {
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown", ".pages", ".tex"},
    "Spreadsheets": {".xls", ".xlsx", ".ods", ".csv", ".tsv", ".numbers"},
    "Presentations": {".ppt", ".pptx", ".odp", ".key"},
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".raw", ".ico"},
    "Audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"},
    "Video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v"},
    "Archives": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"},
}
# Never moved by organize_files: unfinished downloads, temporary and Office lock files.
IN_PROGRESS = re.compile(r"(?:\.(?:crdownload|part|partial|download|tmp|temp)$|^~\$|^\.nova-copy-)", re.I)


def _folder(value, env):
    try:
        return nova_actions._folder_param(value, env.services, False)
    except nt.PlanError as exc:
        raise na.ToolError(str(exc)) from None


def _file(value, env, label="The file"):
    try:
        return str(nova_actions.checked_file(value, env.services, label))
    except nt.PlanError as exc:
        raise na.ToolError(str(exc)) from None


def _recheck(kind, path, ctx, need):
    """nova_actions' run-time re-check (everything again, right before use), as a tool error."""
    try:
        return (nova_actions.recheck_folder if kind == "folder" else nova_actions.recheck_file)(path, ctx, need)
    except nt.ActionError as exc:
        raise na.ToolError(str(exc)) from None


def _unapproved(path, need, env):
    root = nt.approval_needed(str(path), need, env.services, env.approved)
    return [root] if root else []


def _model_path(value, trusted):
    """Why a confirmation is needed for a path the AI model wrote itself (not the user, not Nova)."""
    if trusted or str(value).strip().lower() in (*nt.KNOWN_FOLDERS, nt.ASK):
        return None
    return "uses a path suggested by the AI model"


def _hidden(path):
    try:
        attributes = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


class FileTool(na.Tool):
    retry_on_timeout = True

    def available(self, env):
        return (env.services is not None, "file tools need Nova's task services")

    def target(self, args):
        return str(args.get("folder") or args.get("file") or "")


class ListFolder(FileTool):
    name, title = "list_folder", "List a folder"
    description = ("List the files and subfolders in a folder (name, size, date). Optional pattern: part of a "
                   "name or a wildcard like *.pdf. Read-only.")
    input_schema = na.schema({"folder": na.string("downloads, documents, desktop, pictures, ask, or a full folder "
                                                  "path"),
                              "pattern": na.string("optional: part of a name, or a wildcard like *.pdf", max_length=100)},
                             ["folder"])
    outputs = "files (list of full paths), folders (list of full paths), entries (list of {name, kind, size, modified})"
    safety = na.READ_ONLY
    timeout = 20.0

    def prepare(self, args, env, trusted=False):
        folder = _folder(args["folder"], env)
        pattern = str(args.get("pattern") or "").strip()
        if any(ch in pattern for ch in "\\/:"):
            raise na.ToolError("The pattern is part of a name or a wildcard, without folders.")
        return na.Prepared({"folder": folder, "pattern": pattern},
                           f"List {folder}" + (f" (names matching “{pattern}”)" if pattern else ""), na.READ_ONLY,
                           confirm=_model_path(args["folder"], trusted), unapproved=_unapproved(folder, "read", env))

    def execute(self, prepared, ctx, env):
        import fnmatch
        folder, pattern = Path(prepared.params["folder"]), prepared.params["pattern"].lower()
        _recheck("folder", folder, ctx, "read")
        entries, files, folders, total = [], [], [], 0
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError as exc:
            raise na.ToolError(f"Windows didn't let Nova list {folder} ({exc.strerror or exc}).") from None
        for name in names:
            if ctx.stopped():
                raise na.ToolError("Stopped.")
            if pattern and not (fnmatch.fnmatch(name.lower(), pattern) if any(c in pattern for c in "*?[")
                                else pattern in name.lower()):
                continue
            path = folder / name
            try:
                nova_actions.check_not_protected(path, env.services)
                info = os.lstat(path)
            except (nt.PlanError, OSError):
                continue                                  # keys, credentials, Nova's data: not listed
            total += 1
            if len(entries) >= MAX_LIST:
                continue
            kind = "link" if nova_actions.is_link(path) else "folder" if stat.S_ISDIR(info.st_mode) else "file"
            entries.append({"name": name, "kind": kind, "size": info.st_size if kind == "file" else None,
                            "modified": datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "hidden": _hidden(path)})
            (files if kind == "file" else folders if kind == "folder" else []).append(str(path))
        more = f" (showing the first {MAX_LIST} of {total})" if total > MAX_LIST else ""
        lines = [f"  • {e['name']}" + ("\\" if e["kind"] == "folder" else f" ({e['size']:,} bytes)" if e["size"]
                                       is not None else " (link, not followed)") for e in entries[:30]]
        return na.Output(f"{len(files)} file(s) and {len(folders)} folder(s) in {folder}{more}"
                         + (":\n" + "\n".join(lines) if lines else ".") + ("\n  …" if len(entries) > 30 else ""),
                         {"folder": str(folder), "files": files, "folders": folders, "entries": entries,
                          "truncated": total > MAX_LIST})

    def verify(self, prepared, output, env):
        return isinstance(output.data.get("entries"), list), "listed from disk just now"

    def summary(self, output):
        return output.message.splitlines()[0]


class ReadTextFile(FileTool):
    name, title = "read_text_file", "Read a text file"
    description = ("Read the text of a .txt, .md, .csv, .tsv, .log or .json file so it can be used in later steps "
                   "or the answer. Read-only; the contents are never stored in the task history.")
    input_schema = na.schema({"file": na.string("ask, or a full file path"),
                              "max_chars": na.integer("how much to read (default 4000)", 100, MAX_READ_CHARS)},
                             ["file"])
    outputs = "file, text, characters, truncated"
    safety = na.READ_ONLY
    timeout = 15.0

    def prepare(self, args, env, trusted=False):
        file = _file(args["file"], env)
        if Path(file).suffix.lower() not in READABLE:
            raise na.ToolError(f"Nova reads {', '.join(sorted(READABLE))} files here (use read_document for PDF, "
                               "Word and Excel files).")
        return na.Prepared({"file": file, "max_chars": int(args.get("max_chars") or DEFAULT_READ_CHARS)},
                           f"Read the text of {file}", na.READ_ONLY, confirm=_model_path(args["file"], trusted),
                           unapproved=_unapproved(file, "read", env))

    def execute(self, prepared, ctx, env):
        path = _recheck("file", prepared.params["file"], ctx, "read")
        size = os.path.getsize(path)
        if size > MAX_READ_BYTES:
            raise na.ToolError(f"{path.name} is larger than {MAX_READ_BYTES // 1024 ** 2} MB; Nova reads smaller "
                               "text files here.")
        limit = prepared.params["max_chars"]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise na.ToolError(f"Windows didn't let Nova read {path.name} ({exc.strerror or exc}).",
                               transient="being used" in str(exc)) from None
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16", errors="replace")
        else:
            text = raw.decode("utf-8-sig", errors="replace")
        if "\x00" in text[:2000]:
            raise na.ToolError(f"{path.name} doesn't look like a text file.")
        excerpt = na._clean(text[:limit * 2], limit)        # instructions planted in files are removed
        return na.Output(f"Read {path.name} ({len(text):,} characters" + (", the first part only" if len(text) > limit
                                                                         else "") + ").",
                         {"file": str(path), "text": excerpt, "characters": len(text),
                          "truncated": len(text) > limit})

    def verify(self, prepared, output, env):
        return True, "read from disk just now"


class OrganizeFiles(FileTool):
    name, title = "organize_files", "Organize a folder's files into subfolders"
    description = ("Sort the files directly in a folder into subfolders by “type” (Documents, Images, Video…), "
                   "“extension” (PDF, DOCX…) or “month” (2026-11). Optional pattern limits which files. Shows a "
                   "preview and always asks first; never replaces files; programs and scripts are left alone.")
    input_schema = na.schema({"folder": na.string("downloads, documents, desktop, pictures, ask, or a full folder "
                                                  "path"),
                              "by": na.string("how to sort", ["type", "extension", "month"]),
                              "pattern": na.string("optional: part of a name, or a wildcard like *.pdf",
                                                   max_length=100)}, ["folder"])
    outputs = "moved (list of {source, file}), skipped (list of {file, reason}), folder"
    safety = na.CONSEQUENTIAL
    timeout = 300.0
    retry_on_timeout = False

    @staticmethod
    def subfolder(path, by):
        suffix = path.suffix.lower()
        if by == "extension":
            return suffix[1:].upper() if suffix else "No extension"
        if by == "month":
            return datetime.fromtimestamp(os.lstat(path).st_mtime).strftime("%Y-%m")
        return next((name for name, kinds in CATEGORIES.items() if suffix in kinds), "Other")

    def proposal(self, folder, by, pattern, services):
        """(moves [(source, subfolder)], skipped [(name, reason)], how many weren't included)."""
        import fnmatch
        moves, skipped = [], []
        pattern = pattern.lower()
        for name in sorted(os.listdir(folder), key=str.lower):
            path = Path(folder) / name
            if not os.path.isfile(path) or nova_actions.is_link(path):
                continue                                   # subfolders and links stay as they are
            if pattern and not (fnmatch.fnmatch(name.lower(), pattern) if any(c in pattern for c in "*?[")
                                else pattern in name.lower()):
                continue
            reason = None
            if _hidden(path) or name.startswith("."):
                reason = "hidden or system file"
            elif IN_PROGRESS.search(name):
                reason = "still downloading, temporary or open in Office"
            elif path.suffix.lower() in nova_actions.RUNNABLE:
                reason = "a program, script or shortcut (Nova doesn't move those)"
            else:
                try:
                    nova_actions.check_not_protected(path, services)
                except nt.PlanError:
                    reason = "looks like a key, credential or Nova data file"
            if reason:
                skipped.append((name, reason))
                continue
            sub = self.subfolder(path, by)
            target_dir = Path(folder) / sub
            if os.path.lexists(target_dir) and (not target_dir.is_dir() or nova_actions.is_link(target_dir)):
                skipped.append((name, f"“{sub}” exists but isn't a plain folder"))
            elif os.path.lexists(target_dir / name):
                skipped.append((name, f"a file with that name is already in “{sub}”"))
            else:
                moves.append((str(path), sub))
        extra = max(0, len(moves) - MAX_ORGANIZE)
        return moves[:MAX_ORGANIZE], skipped, extra

    def prepare(self, args, env, trusted=False):
        folder = _folder(args["folder"], env)
        by = args.get("by") or "type"
        pattern = str(args.get("pattern") or "").strip()
        if any(ch in pattern for ch in "\\/:"):
            raise na.ToolError("The pattern is part of a name or a wildcard, without folders.")
        try:
            moves, skipped, extra = self.proposal(folder, by, pattern, env.services)
        except OSError as exc:
            raise na.ToolError(f"Windows didn't let Nova list {folder} ({exc.strerror or exc}).") from None
        if not moves:
            why = f" ({len(skipped)} file(s) were left alone: {skipped[0][1]}…)" if skipped else ""
            raise na.ToolError(f"There's nothing to organize in {folder}{why}.")
        groups = sorted({sub for _src, sub in moves})
        lines = [f"{Path(src).name} → {sub}\\" for src, sub in moves[:15]]
        if len(moves) > 15:
            lines.append(f"… and {len(moves) - 15} more")
        if skipped:
            lines.append(f"Left alone: {len(skipped)} file(s) (e.g. {skipped[0][0]}: {skipped[0][1]})")
        if extra:
            lines.append(f"{extra} more file(s) are left for another run (at most {MAX_ORGANIZE} at a time).")
        reason = (f"moves {len(moves)} file(s) into {len(groups)} subfolder(s) of {folder} — a bulk change. Every move "
                  "is recorded in the activity log")
        model = _model_path(args["folder"], trusted)
        return na.Prepared({"folder": folder, "by": by, "pattern": pattern, "moves": moves},
                           f"Organize {len(moves)} file(s) in {folder} by {by} into: {', '.join(groups[:6])}"
                           + ("…" if len(groups) > 6 else ""), na.CONSEQUENTIAL,
                           confirm=reason + (f"; it {model}" if model else ""), preview="\n".join(lines),
                           unapproved=_unapproved(folder, "change", env))

    def execute(self, prepared, ctx, env):
        folder = _recheck("folder", prepared.params["folder"], ctx, "change")
        moved, skipped, created = [], [], []
        for source, sub in prepared.params["moves"]:          # exactly what was previewed and approved
            if ctx.stopped():
                break
            name = Path(source).name
            try:
                path = nova_actions.recheck_file(source, ctx, "change")
                if path.parent != folder:
                    raise nt.ActionError("it isn't directly in the folder any more")
                target_dir = folder / sub
                if not os.path.lexists(target_dir):
                    os.mkdir(target_dir)
                    created.append(str(target_dir))
                if not target_dir.is_dir() or nova_actions.is_link(target_dir):
                    raise nt.ActionError(f"“{sub}” isn't a plain folder")
                target = target_dir / name
                os.rename(path, target)                         # never replaces an existing file
                if not target.is_file() or os.path.lexists(path):
                    raise nt.ActionError("the move couldn't be confirmed")
            except FileExistsError:
                skipped.append({"file": source, "reason": f"a file with that name appeared in “{sub}”"})
                na.record_audit(env.connect, env.run_id, self.name, "move", source, "skipped", "target exists")
                continue
            except (nt.ActionError, OSError) as exc:
                skipped.append({"file": source, "reason": str(exc)})
                na.record_audit(env.connect, env.run_id, self.name, "move", source, "failed", str(exc))
                continue
            moved.append({"source": source, "file": str(target)})
            na.record_audit(env.connect, env.run_id, self.name, "move", f"{source} → {target}", "succeeded")
        stopped = ctx.stopped() and len(moved) + len(skipped) < len(prepared.params["moves"])
        if not moved:
            raise na.ToolError("Stopped before any file was moved." if stopped else
                               "No file could be moved: " + (skipped[0]["reason"] if skipped else "unknown reason"))
        message = (f"Moved {len(moved)} file(s) into {len({Path(m['file']).parent.name for m in moved})} subfolder(s) "
                   f"of {folder}" + (f"; {len(skipped)} skipped" if skipped else "")
                   + ("; stopped before the rest — they're still where they were" if stopped else "") + ".")
        return na.Output(message, {"folder": str(folder), "moved": moved, "skipped": skipped, "created": created,
                                   "stopped": stopped})

    def verify(self, prepared, output, env):
        missing = [m for m in output.data["moved"] if not os.path.isfile(m["file"]) or os.path.lexists(m["source"])]
        if missing:
            return False, f"{len(missing)} moved file(s) aren't where they should be"
        return True, f"checked on disk: {len(output.data['moved'])} file(s) are in their new subfolders and gone " \
                     "from the old place"

    def summary(self, output):
        return output.message


def tools():
    return [ListFolder(), ReadTextFile(), OrganizeFiles()]


# ==================== planner rules and intent ====================
def parse_clause(text, context):
    m = re.fullmatch(r"(?:list|show)\s+(?:me\s+)?(?:(?:the|all|my)\s+)*(?:files|contents?|everything)?\s*"
                     r"(?:in|of|inside)?\s*(?P<where>.+)", text, re.I)
    if m and re.match(r"(?:list|show)\s+(?:me\s+)?(?:(?:the|all|my)\s+)*(?:files|contents?|everything)\b", text, re.I):
        folder = nt._folder_expr(m.group("where"), context)
        if folder is not None:
            context["folder"] = folder
            return [("list_folder", {"folder": folder})]
    m = re.fullmatch(r"(?:organi[sz]e|tidy(?:\s+up)?|sort|clean\s+up)\s+(?:the\s+files\s+in\s+)?(?P<where>.+?)"
                     r"(?:\s+(?:by|into\s+folders\s+by)\s+(?P<by>type|kind|extension|month|date))?", text, re.I)
    if m:
        folder = nt._folder_expr(m.group("where"), context)
        if folder is not None:
            by = {"kind": "type", "date": "month"}.get((m.group("by") or "type").lower(), (m.group("by") or "type").lower())
            return [("organize_files", {"folder": folder, "by": by})]
    return None


na.CLAUSE_RULES.append(parse_clause)
na.INTENT["organize_files"] = re.compile(
    r"\b(?:organi[sz]\w*|tid(?:y|ied|ying)|sort\w*|clean\w*\s+up|arrang\w*|group\w*)\b", re.I)

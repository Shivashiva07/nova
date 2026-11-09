"""
Shared reliability and safety helpers for Nova: version, atomic file writes,
a folder walker that can't escape or loop, log redaction, and exception
logging for threads. Standard library only; no GUI code.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import sys
import tempfile
import threading
from pathlib import Path

NOVA_VERSION = "1.24.0"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400   # symlinks, junctions and other redirections on Windows


# ==================== lazy imports and memory ====================
class LazyModule:
    """A module that is imported on first use, to keep startup fast.

    Attribute access imports it (thread-safely); available() reports whether
    it can be imported at all, without raising. on_load(module) runs once.
    """

    def __init__(self, name, on_load=None, errors=(ImportError,)):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_module", None)
        object.__setattr__(self, "_on_load", on_load)
        object.__setattr__(self, "_errors", errors)
        object.__setattr__(self, "_lock", threading.Lock())

    def _load(self):
        module = object.__getattribute__(self, "_module")
        if module is None:
            with object.__getattribute__(self, "_lock"):
                module = object.__getattribute__(self, "_module")
                if module is None:
                    import importlib
                    module = importlib.import_module(object.__getattribute__(self, "_name"))
                    object.__setattr__(self, "_module", module)
                    hook = object.__getattribute__(self, "_on_load")
                    if hook:
                        try:
                            hook(module)
                        except Exception:
                            logging.getLogger("nova").exception("Setting up %s failed", module.__name__)
        return module

    def available(self):
        try:
            self._load()
            return True
        except object.__getattribute__(self, "_errors"):
            return False

    def loaded(self):
        return object.__getattribute__(self, "_module") is not None

    def __getattr__(self, attr):
        return getattr(self._load(), attr)

    def __repr__(self):
        return f"<lazy module {object.__getattribute__(self, '_name')}>"


def working_set_mb():
    """This process's memory in use (Windows working set), in MB, or None."""
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / 2 ** 20
    except Exception:
        return None


# ==================== atomic writes ====================
def atomic_write_bytes(path, data):
    """Write a file so that readers see either the old or the new content, never a mix.

    The data goes to a temporary file in the same folder, is flushed to disk,
    then renamed over the target. On any failure the original is untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return path


def atomic_write_text(path, text, encoding="utf-8"):
    return atomic_write_bytes(path, str(text).encode(encoding))


# ==================== safe folder walking ====================
def is_redirect(entry_or_path):
    """True for symlinks, Windows junctions and other reparse points."""
    try:
        info = (entry_or_path.stat(follow_symlinks=False) if isinstance(entry_or_path, os.DirEntry)
                else os.lstat(entry_or_path))
    except OSError:
        return True  # can't inspect it: treat as unsafe
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def safe_walk(root, onerror=None):
    """Like os.walk(root), but never follows symlinks or junctions and never
    visits the same folder twice, so it can't loop or leave `root`.

    Yields (dirpath, dirnames, filenames); prune dirnames in place to skip
    folders, as with os.walk. Linked files are skipped too.
    """
    root = os.fspath(root)
    if is_redirect(root):
        return
    stack, seen = [root], set()
    while stack:
        current = stack.pop()
        try:
            info = os.stat(current)
            key = (info.st_dev, info.st_ino)
            if key in seen and info.st_ino:
                continue
            seen.add(key)
            with os.scandir(current) as entries:
                listed = list(entries)
        except OSError as exc:
            if onerror:
                onerror(exc)
            continue
        dirnames, filenames = [], []
        for entry in listed:
            try:
                if is_redirect(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirnames.append(entry.name)
                elif entry.is_file(follow_symlinks=False):
                    filenames.append(entry.name)
            except OSError:
                continue
        dirnames.sort()
        filenames.sort()
        yield current, dirnames, filenames
        stack.extend(os.path.join(current, name) for name in reversed(dirnames))


# ==================== log redaction ====================
_REDACTIONS = (
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|gh[ousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
                r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b"), "[redacted key]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
                r"(\s*[:=]\s*|\s+is\s+)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[number]"),
)
MAX_LOG_RECORD_CHARS = 4000


def redact(text, limit=MAX_LOG_RECORD_CHARS):
    """Mask secrets (keys, tokens, passwords, URL credentials), emails and long
    card-like numbers, and cap the length so whole documents can't land in a log."""
    text = str(text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[:limit] + f"… [{len(text) - limit} more characters not logged]"
    return text


class RedactingFormatter(logging.Formatter):
    """Formats a record (including any traceback), then redacts and caps it."""

    def format(self, record):
        return redact(super().format(record))


def install_exception_hooks(logger):
    """Log otherwise-silent crashes: uncaught errors in the main thread and in worker threads."""
    previous = sys.excepthook

    def main_hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("Unhandled error", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "unknown thread"
        logger.error("Unhandled error in background thread %s", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_hook
    threading.excepthook = thread_hook

"""
Windows desktop automation for Nova's agent (Step 30). No GUI code.

Lets the agent find, focus, read, type into, click in and close the windows of
approved apps, through tools registered with the Step 28 engine:

  list_windows     which approved apps have windows open (read-only)
  focus_window     bring an app's window to the front and check that it is active
  read_window      read a window's text and controls through UI Automation (read-only)
  type_text        type text into Notepad or Calculator, checking focus as it goes
  press_keys       send an allowed keyboard shortcut (Ctrl+S and clipboard keys ask first)
  click_control    press a button, menu item, tab… found by name through UI Automation
  close_app        ask an app's window to close (never kills it; asks first)

Opening apps reuses the Step 20 action open_app; websites and folders keep
their Step 20/25 actions; web pages are driven by nova_browser.

Safety model
- Only the apps in POLICIES, matched by their program file (nova_actions'
  window matching), never any window by title alone. Each app has its own
  list of what may be done: typing only into Notepad (any text) and Calculator
  (digits and operators); no typing or clicking in VS Code (its terminal runs
  commands), File Explorer (clicks could change files outside allowed
  folders) or the browser (nova_browser does web pages).
- Shortcuts come from a fixed list; Alt+F4, Win+…, Alt+Tab and anything else
  is refused. Saving, closing tabs and clipboard shortcuts ask first.
- Before every interaction the window must exist, belong to the app, have no
  unexpected dialog open, and be the active window (Windows' focus rules are
  never worked around). Typing re-checks focus every few characters and stops
  at once if focus moves, a modifier key is held down, or the task is stopped.
- Controls are found by name through Windows UI Automation and must be enabled
  and on screen. A control is pressed through UI Automation; a mouse click is
  only a fallback, and only after checking that exactly that control is at that
  point right now. Screen coordinates are never stored or replayed.
- Text that looks like a password or key is never typed.
- Results are checked: typed text is read back, the active window is read
  from Windows, a closed window must be gone.
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import nova_actions
import nova_agent as na
import nova_tasks as nt

log = logging.getLogger("nova.desktop")

WINDOW_WAIT = 10.0          # seconds to wait for a window to appear (e.g. right after open_app)
MAX_WAIT = 30
TYPE_CHUNK = 12             # characters typed between checks that the window still has the focus
MAX_TYPE_CHARS = 2000
CLOSE_WAIT = 5.0
MAX_CONTROLS = 120
UIA_DEADLINE = 3.0          # seconds for walking a window's controls


class AutomationError(Exception):
    """A desktop operation failed; the message is shown to the user."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


# ==================== what may be done in which app ====================
# combo -> (what it does, why Nova asks first or None)
KEYS = {
    "ctrl+a": ("select all", None), "ctrl+z": ("undo", None), "ctrl+y": ("redo", None),
    "ctrl+n": ("new document or window", None), "ctrl+t": ("new tab", None), "ctrl+f": ("find", None),
    "ctrl+home": ("go to the start", None), "ctrl+end": ("go to the end", None),
    "ctrl+shift+s": ("open the Save As dialog", None),
    "home": ("Home", None), "end": ("End", None), "enter": ("Enter", None), "tab": ("Tab", None),
    "escape": ("Esc", None), "backspace": ("Backspace", None), "delete": ("Delete", None),
    "up": ("Up arrow", None), "down": ("Down arrow", None), "left": ("Left arrow", None),
    "right": ("Right arrow", None), "page up": ("Page Up", None), "page down": ("Page Down", None),
    "ctrl+s": ("save", "saves the document, which may replace the file on disk"),
    "ctrl+c": ("copy", "replaces what's on your clipboard"),
    "ctrl+x": ("cut", "removes the selection and replaces what's on your clipboard"),
    "ctrl+v": ("paste", "pastes what's on your clipboard into the app"),
    "ctrl+w": ("close the tab", "closes the tab (the app asks if anything is unsaved)"),
}
KEY_ALIASES = {"control": "ctrl", "ctl": "ctrl", "esc": "escape", "return": "enter", "del": "delete",
               "pgup": "page up", "pgdn": "page down", "pageup": "page up", "pagedown": "page down",
               "arrow up": "up", "arrow down": "down", "arrow left": "left", "arrow right": "right",
               "up arrow": "up", "down arrow": "down", "left arrow": "left", "right arrow": "right"}
EDITOR_KEYS = frozenset(KEYS)
_ANY_TEXT = r"[^\x00-\x08\x0b-\x1f\x7f]"          # printable text, tabs and new lines


@dataclass(frozen=True)
class AppPolicy:
    type_chars: str | None = None      # characters that may be typed (regex for one character); None: no typing
    click: bool = False                # buttons, menu items, tabs… may be pressed
    keys: frozenset = frozenset()      # shortcuts that may be sent
    editor: bool = False               # typed text goes into a text box and can be read back


POLICIES = {
    "notepad": AppPolicy(type_chars=_ANY_TEXT, click=True, keys=EDITOR_KEYS, editor=True),
    "calculator": AppPolicy(type_chars=r"[0-9+\-*/().=,%^ \n]", click=True,
                            keys=frozenset({"escape", "enter", "backspace", "delete", "ctrl+c"})),
    "paint": AppPolicy(click=True, keys=frozenset({"ctrl+z", "ctrl+y", "ctrl+s", "ctrl+n", "escape"})),
    "vscode": AppPolicy(),
    "explorer": AppPolicy(),
    "browser": AppPolicy(),
}
NO_TYPING = {"vscode": "its terminal and command palette can run commands",
             "explorer": "typing there can rename or open files outside your allowed folders",
             "browser": "web pages are handled by Nova's browser tools (browser_type)",
             "paint": "Paint has no text box Nova can check"}
NO_CLICKING = {"vscode": "its buttons can run commands, tasks or extensions",
               "explorer": "clicks there can change files outside your allowed folders",
               "browser": "web pages are handled by Nova's browser tools (browser_click)"}
# Controls whose name suggests a consequential action: pressing them always asks first.
RISKY_CONTROL = re.compile(
    r"\b(?:delete|remove|erase|discard|don'?t\s+save|do\s+not\s+save|send|submit|buy|purchase|pay|order|"
    r"checkout|install|uninstall|format|reset|overwrite|replace|empty|sign\s*out|log\s*out|exit|quit|"
    r"close|yes|ok|apply|accept|agree|confirm|save)\b", re.I)
# Text seen on screen after saving that means it didn't work (Step 31's visual re-check).
FAILURE = re.compile(r"denied|\berror\b|\bfailed\b|(?:could|can)(?:n'?t| ?not)|not responding|unhandled", re.I)
CONTROL_KINDS = ("any", "button", "menu item", "tab", "check box", "radio button", "list item", "hyperlink",
                 "split button", "combo box")


def app_title(app):
    return nova_actions.SWITCH_APPS.get(app, (str(app),))[0]


def normalize_keys(value):
    """“Ctrl + S”, “control+s”, “Esc” -> the KEYS spelling, or None."""
    text = " ".join(str(value or "").lower().replace("-", "+").split())
    text = KEY_ALIASES.get(text, text)
    parts = [p.strip() for p in text.split("+")]
    if len(parts) > 1:
        parts = [KEY_ALIASES.get(p, p) for p in parts]
        mods = sorted((p for p in parts[:-1]), key=lambda m: ("ctrl", "shift", "alt").index(m)
                      if m in ("ctrl", "shift", "alt") else 9)
        text = "+".join(mods + [parts[-1]])
    return text if text in KEYS else None


# ==================== data ====================
@dataclass
class WindowInfo:
    handle: int
    title: str
    app: str
    pid: int = 0
    active: bool = False
    enabled: bool = True               # False while a modal dialog blocks it
    minimized: bool = False
    buttons: list = field(default_factory=list)   # for dialogs: the names of their buttons


@dataclass
class ControlInfo:
    name: str
    kind: str
    automation_id: str = ""
    enabled: bool = True
    on_screen: bool = True
    editable: bool = False
    value: str | None = None


@dataclass
class ClickResult:
    control: ControlInfo
    method: str                        # invoke | toggle | select | expand | mouse


# ==================== the real backend (Windows) ====================
_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "home": 0x24, "end": 0x23, "enter": 0x0D, "tab": 0x09,
       "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
       "page up": 0x21, "page down": 0x22}
_EXTENDED = {0x24, 0x23, 0x2E, 0x26, 0x28, 0x25, 0x27, 0x21, 0x22}
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x2, 0x4
WM_CLOSE = 0x0010
# UI Automation ids
UIA_INVOKE, UIA_SELECTIONITEM, UIA_VALUE, UIA_EXPANDCOLLAPSE, UIA_TOGGLE, UIA_TEXT = (
    10000, 10010, 10002, 10005, 10015, 10014)
UIA_NAME_PROPERTY, UIA_CONTROLTYPE_PROPERTY, UIA_ISDIALOG_PROPERTY = 30005, 30003, 30174
CONTROL_TYPES = {50000: "button", 50002: "check box", 50003: "combo box", 50004: "edit", 50005: "hyperlink",
                 50006: "image", 50007: "list item", 50008: "list", 50009: "menu", 50010: "menu bar",
                 50011: "menu item", 50012: "progress bar", 50013: "radio button", 50014: "scroll bar",
                 50015: "slider", 50016: "spinner", 50017: "status bar", 50018: "tab", 50019: "tab item",
                 50020: "text", 50021: "toolbar", 50022: "tooltip", 50023: "tree", 50024: "tree item",
                 50025: "custom", 50026: "group", 50028: "data grid", 50029: "data item", 50030: "document",
                 50031: "split button", 50032: "window", 50033: "pane", 50034: "header", 50035: "header item",
                 50036: "table", 50037: "title bar", 50038: "separator"}
KIND_TYPES = {"button": {50000}, "menu item": {50011}, "tab": {50019}, "check box": {50002},
              "radio button": {50013}, "list item": {50007}, "hyperlink": {50005}, "split button": {50031},
              "combo box": {50003}}
CLICKABLE_TYPES = {50000, 50002, 50003, 50005, 50007, 50011, 50013, 50019, 50024, 50031}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


_user32 = None
_com = threading.local()


def _u32():
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        for name, args in (("IsWindow", [wintypes.HWND]), ("IsWindowEnabled", [wintypes.HWND]),
                           ("IsWindowVisible", [wintypes.HWND]), ("IsIconic", [wintypes.HWND]),
                           ("SetForegroundWindow", [wintypes.HWND]), ("ShowWindow", [wintypes.HWND, ctypes.c_int]),
                           ("GetWindowTextLengthW", [wintypes.HWND]),
                           ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
                           ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
                           ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]),
                           ("GetWindow", [wintypes.HWND, wintypes.UINT]),
                           ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
                           ("SendInput", [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]),
                           ("GetAsyncKeyState", [ctypes.c_int]), ("SetCursorPos", [ctypes.c_int, ctypes.c_int]),
                           ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)])):
            getattr(u, name).argtypes = args
        u.GetWindow.restype = wintypes.HWND
        u.GetAsyncKeyState.restype = ctypes.c_short
        _user32 = u
    return _user32


def _uia():
    """(module, IUIAutomation) for this thread. COM is initialized once per thread (agent tools run in
    short-lived worker threads); objects are never shared between threads."""
    import comtypes
    import comtypes.client
    if not getattr(_com, "ready", False):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except OSError:
            pass                                        # already initialized on this thread
        _com.ready = True
    module = comtypes.client.GetModule("UIAutomationCore.dll")
    if getattr(_com, "uia", None) is None:
        _com.uia = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
    return module, _com.uia


def _title(hwnd):
    u = _u32()
    length = u.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    u.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _pid(hwnd):
    pid = wintypes.DWORD()
    _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _key_input(vk=0, scan=0, flags=0):
    item = _INPUT(type=INPUT_KEYBOARD)
    item.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return item


def _send(items):
    array = (_INPUT * len(items))(*items)
    sent = _u32().SendInput(len(items), array, ctypes.sizeof(_INPUT))
    if sent != len(items):
        raise AutomationError("Windows didn't accept the keyboard input (another program may be blocking it, "
                              "or the window runs as administrator).")


class WindowsDesktop:
    """Windows APIs and UI Automation. Every method checks state again; nothing is cached between calls."""

    # ---- windows ----
    def windows(self, app):
        u = _u32()
        foreground = u.GetForegroundWindow()
        found = []
        for hwnd, title in nova_actions.app_windows(app):
            found.append(WindowInfo(int(hwnd), title, app, _pid(hwnd), active=hwnd == foreground,
                                    enabled=bool(u.IsWindowEnabled(hwnd)), minimized=bool(u.IsIconic(hwnd))))
        return found

    def foreground(self):
        return int(_u32().GetForegroundWindow() or 0)

    def alive(self, handle):
        return bool(_u32().IsWindow(handle))

    def title(self, handle):
        return _title(handle)

    def focus(self, handle):
        """Bring a window to the front. False if Windows refused (its focus rules are never worked around)."""
        u = _u32()
        if u.IsIconic(handle):
            u.ShowWindow(handle, 9)                     # SW_RESTORE
        u.SetForegroundWindow(handle)
        time.sleep(0.15)
        return int(u.GetForegroundWindow() or 0) == handle

    def close(self, handle):
        """Ask the window to close, as its own close button does (the app may ask to save first)."""
        if not _u32().PostMessageW(handle, WM_CLOSE, 0, 0):
            raise AutomationError("Windows didn't deliver the request to close the window.")

    def dialogs(self, window):
        """Dialogs the app shows over this window: owned top-level windows, and dialogs drawn inside it."""
        u = _u32()
        found = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32 = ctypes.WinDLL("user32")
        user32.EnumWindows.argtypes = [proc, wintypes.LPARAM]

        def visit(hwnd, _lparam):
            if u.IsWindowVisible(hwnd) and u.GetWindow(hwnd, 4) == window.handle:       # GW_OWNER
                found.append(WindowInfo(int(hwnd), _title(hwnd), window.app, _pid(hwnd)))
            return True

        user32.EnumWindows(proc(visit), 0)
        if not found and POLICIES.get(window.app, AppPolicy()).click:
            found += self._inner_dialogs(window)
        for dialog in found:
            dialog.buttons = [c.name for c in self.controls(dialog.handle, limit=40, dialog=dialog.handle ==
                                                            window.handle) if c.kind == "button" and c.name][:8]
        return found

    def _inner_dialogs(self, window):
        """Dialogs some apps (Windows 11 Notepad) draw inside their own window: elements marked as dialogs."""
        try:
            module, uia = _uia()
            root = uia.ElementFromHandle(window.handle)
            condition = uia.CreatePropertyCondition(UIA_ISDIALOG_PROPERTY, True)
            items = root.FindAll(4, condition)                                  # TreeScope_Descendants
            return [WindowInfo(window.handle, items.GetElement(i).CurrentName or "a dialog", window.app, window.pid)
                    for i in range(min(items.Length, 3))]
        except Exception as exc:
            log.info("Couldn't look for dialogs inside the window: %s", exc)
            return []

    # ---- controls ----
    def _element(self, handle, dialog=False):
        module, uia = _uia()
        root = uia.ElementFromHandle(handle)
        if dialog:                                       # a dialog drawn inside the window
            condition = uia.CreatePropertyCondition(UIA_ISDIALOG_PROPERTY, True)
            inner = root.FindFirst(4, condition)
            if inner:
                return module, uia, inner
        return module, uia, root

    def _info(self, module, element):
        kind = CONTROL_TYPES.get(element.CurrentControlType, "control")
        editable, value = False, None
        try:
            pattern = element.GetCurrentPattern(UIA_VALUE)
            if pattern:
                value_pattern = pattern.QueryInterface(module.IUIAutomationValuePattern)
                value = value_pattern.CurrentValue
                editable = not value_pattern.CurrentIsReadOnly
        except Exception:
            pass
        if not editable and kind in ("document", "edit"):
            try:
                editable = bool(element.GetCurrentPattern(UIA_TEXT)) and element.CurrentIsKeyboardFocusable
            except Exception:
                pass
        return ControlInfo(element.CurrentName or "", kind, element.CurrentAutomationId or "",
                           bool(element.CurrentIsEnabled), not element.CurrentIsOffscreen, editable, value)

    def _walk(self, root, uia, limit):
        """Breadth-first over the control view, with a count and time limit (big apps have huge trees)."""
        walker, queue, seen, deadline = uia.ControlViewWalker, [root], [], time.monotonic() + UIA_DEADLINE
        while queue and len(seen) < limit and time.monotonic() < deadline:
            element = queue.pop(0)
            child = walker.GetFirstChildElement(element)
            while child and len(seen) < limit:
                seen.append(child)
                queue.append(child)
                child = walker.GetNextSiblingElement(child)
        return seen

    def controls(self, handle, limit=MAX_CONTROLS, dialog=False):
        module, uia, root = self._element(handle, dialog)
        return [self._info(module, e) for e in self._walk(root, uia, limit)]

    def _find(self, module, uia, root, name, kind):
        wanted = KIND_TYPES.get(kind) if kind and kind != "any" else None
        condition = uia.CreatePropertyConditionEx(UIA_NAME_PROPERTY, name, 1)      # ignore case
        items = root.FindAll(4, condition)
        exact = [items.GetElement(i) for i in range(items.Length)]
        if not exact:                                     # a looser match: the name contains the words
            low = name.lower()
            exact = [e for e in self._walk(root, uia, 400) if low in (e.CurrentName or "").lower()]
        if wanted:
            exact = [e for e in exact if e.CurrentControlType in wanted]
        usable = [e for e in exact if e.CurrentIsEnabled and not e.CurrentIsOffscreen]
        clickable = [e for e in usable if e.CurrentControlType in CLICKABLE_TYPES]
        return clickable or usable, exact

    def click(self, handle, name, kind=None, dialog=False):
        """Press one control found by name: through UI Automation, or as a fallback a mouse click after
        checking that exactly that control is under the point now."""
        module, uia, root = self._element(handle, dialog)
        usable, every = self._find(module, uia, root, name, kind)
        if not every:
            raise AutomationError(f"There's no control called “{name}” in the window.")
        if not usable:
            control = self._info(module, every[0])
            state = "disabled" if not control.enabled else "not on screen"
            raise AutomationError(f"“{control.name}” is {state}, so Nova didn't click it.")
        if len(usable) > 1:
            names = {(e.CurrentName, CONTROL_TYPES.get(e.CurrentControlType, "control")) for e in usable}
            if len(names) > 1 or len(usable) > 3:
                listed = ", ".join(f"“{n}” ({k})" for n, k in sorted(names)[:5])
                raise AutomationError(f"More than one control matches “{name}”: {listed}. Use its exact name.")
        element = usable[0]
        info = self._info(module, element)
        for pattern_id, interface, method in ((UIA_INVOKE, "IUIAutomationInvokePattern", "Invoke"),
                                              (UIA_TOGGLE, "IUIAutomationTogglePattern", "Toggle"),
                                              (UIA_SELECTIONITEM, "IUIAutomationSelectionItemPattern", "Select"),
                                              (UIA_EXPANDCOLLAPSE, "IUIAutomationExpandCollapsePattern", "Expand")):
            pattern = element.GetCurrentPattern(pattern_id)
            if pattern:
                getattr(pattern.QueryInterface(getattr(module, interface)), method)()
                return ClickResult(info, method.lower() if method != "Invoke" else "invoke")
        return ClickResult(info, self._mouse_click(module, uia, element, handle))

    def _mouse_click(self, module, uia, element, handle):
        rect = element.CurrentBoundingRectangle
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise AutomationError("The control has no area on screen, so Nova didn't click it.")
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        if self.foreground() != handle:
            raise AutomationError("The window isn't active any more, so Nova didn't click.")
        at_point = uia.ElementFromPoint(module.tagPOINT(x, y))
        same, walker, current = False, uia.ControlViewWalker, at_point
        for _ in range(6):                                # the control itself, or a part of it
            if current is None or not current:
                break
            if uia.CompareElements(current, element):
                same = True
                break
            current = walker.GetParentElement(current)
        if not same:
            raise AutomationError("Something else is on top of that control right now, so Nova didn't click.")
        u = _u32()
        old = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(old))
        u.SetCursorPos(x, y)
        down, up = _INPUT(type=INPUT_MOUSE), _INPUT(type=INPUT_MOUSE)
        down.union.mi = _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)
        up.union.mi = _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)
        try:
            _send([down, up])
        finally:
            u.SetCursorPos(old.x, old.y)
        return "mouse"

    def focused(self, handle):
        """The control with the keyboard focus, if it belongs to this window's program; else None."""
        module, uia = _uia()
        element = uia.GetFocusedElement()
        if not element or element.CurrentProcessId != _pid(handle):
            return None
        return self._info(module, element)

    def read_text(self, handle, focused_only=False):
        """The text of the window's text box (the focused one if asked), or the names of its text items."""
        module, uia = _uia()
        candidates = []
        if focused_only:
            element = uia.GetFocusedElement()
            if element and element.CurrentProcessId == _pid(handle):
                candidates.append(element)
        else:
            root = uia.ElementFromHandle(handle)
            for type_id in (50030, 50004):                # document, then edit
                found = root.FindFirst(4, uia.CreatePropertyCondition(UIA_CONTROLTYPE_PROPERTY, type_id))
                if found:
                    candidates.append(found)
        for element in candidates:
            try:
                pattern = element.GetCurrentPattern(UIA_TEXT)
                if pattern:
                    return pattern.QueryInterface(module.IUIAutomationTextPattern).DocumentRange.GetText(20000)
                pattern = element.GetCurrentPattern(UIA_VALUE)
                if pattern:
                    return pattern.QueryInterface(module.IUIAutomationValuePattern).CurrentValue or ""
            except Exception as exc:
                log.info("Couldn't read a text box: %s", exc)
        if focused_only:
            return None
        root = uia.ElementFromHandle(handle)
        texts = [e.CurrentName for e in self._walk(root, uia, 200)
                 if e.CurrentControlType == 50020 and e.CurrentName]
        return "\n".join(texts[:30])

    # ---- keyboard ----
    def modifiers_held(self):
        u = _u32()
        return any(u.GetAsyncKeyState(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def _wait_for_modifiers(self):
        deadline = time.monotonic() + 1.5
        while self.modifiers_held():
            if time.monotonic() > deadline:
                raise AutomationError("A Ctrl, Shift, Alt or Windows key is held down, so Nova didn't type "
                                      "(the keys would have become shortcuts).")
            time.sleep(0.05)

    def type_text(self, handle, text, should_stop):
        """Type text in small chunks; before each chunk the window must still be active and no modifier
        key held. Returns the number of characters typed."""
        typed = 0
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for start in range(0, len(text), TYPE_CHUNK):
            if should_stop():
                raise AutomationError(f"Stopped after typing {typed} of {len(text)} characters.")
            if self.foreground() != handle:
                raise AutomationError(f"The window lost the focus after {typed} of {len(text)} characters, "
                                      "so Nova stopped typing (the rest could have gone to another window).")
            self._wait_for_modifiers()
            items = []
            for ch in text[start:start + TYPE_CHUNK]:
                if ch == "\n":
                    items += [_key_input(0x0D), _key_input(0x0D, flags=KEYEVENTF_KEYUP)]
                elif ch == "\t":
                    items += [_key_input(0x09), _key_input(0x09, flags=KEYEVENTF_KEYUP)]
                else:
                    for unit in ch.encode("utf-16-le").hex(" ", 2).split():   # surrogate pairs: two units
                        code = int.from_bytes(bytes.fromhex(unit), "little")
                        items += [_key_input(scan=code, flags=KEYEVENTF_UNICODE),
                                  _key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
            _send(items)
            typed += len(text[start:start + TYPE_CHUNK])
            time.sleep(0.02)
        return typed

    def send_keys(self, handle, combo):
        if self.foreground() != handle:
            raise AutomationError("The window isn't active, so Nova didn't send the keys.")
        self._wait_for_modifiers()
        parts = combo.split("+")
        codes = [_VK[p] if p in _VK else ord(p.upper()) for p in parts]
        items = [_key_input(c, flags=KEYEVENTF_EXTENDEDKEY if c in _EXTENDED else 0) for c in codes]
        items += [_key_input(c, flags=KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if c in _EXTENDED else 0))
                  for c in reversed(codes)]
        _send(items)                                    # key-ups in the same call: nothing stays held down


# ==================== emergency stop hotkey ====================
class EmergencyHotkey:
    """Ctrl+Alt+Backspace anywhere in Windows calls on_press (registered only while a task runs).
    If another program already uses the combination, registered stays False; the Stop buttons still work."""
    MODIFIERS, KEY, LABEL = 0x0002 | 0x0001 | 0x4000, 0x08, "Ctrl+Alt+Backspace"   # CONTROL|ALT|NOREPEAT

    def __init__(self, on_press):
        self.on_press, self.registered = on_press, False
        self._thread, self._thread_id, self._ready = None, None, threading.Event()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self.registered
        self._ready.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="NovaEmergencyHotkey")
        self._thread.start()
        self._ready.wait(2)
        return self.registered

    def _loop(self):
        user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._thread_id = kernel32.GetCurrentThreadId()
        self.registered = bool(user32.RegisterHotKey(None, 1, self.MODIFIERS, self.KEY))
        self._ready.set()
        if not self.registered:
            log.info("The emergency-stop hotkey %s is in use by another program", self.LABEL)
            return
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:                 # WM_HOTKEY
                    try:
                        self.on_press()
                    except Exception as exc:
                        log.warning("Emergency stop handler failed: %s", exc)
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._thread is not None and self._thread_id:
            user32 = ctypes.WinDLL("user32")
            user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)          # WM_QUIT
            self._thread.join(1)
        self._thread, self._thread_id, self.registered = None, None, False
        self._ready.clear()


# ==================== agent tools ====================
APPS = list(POLICIES)
_OFFER = re.compile(
    r"\b(?:windows?|apps?|notepad|calculator|calc|paint|mspaint|vs\s?code|vscode|visual\s+studio\s+code|code|"
    r"explorer|browser|edge|chrome|firefox|typ\w*|click\w*|press\w*|shortcut|ctrl|close|quit|focus|switch)\b", re.I)


def _app_arg(description="app"):
    return na.string(description, APPS)


def _title_arg():
    return na.string("optional: part of the window title, to pick one window", max_length=200)


class DesktopTool(na.Tool):
    """Common checks: automation is on, the app is approved, its window is found, ready and active."""
    retry_on_timeout = False

    def available(self, env):
        if env.desktop is None:
            return False, "desktop automation isn't available"
        if not env.settings.get("desktop_automation", True):
            return False, "controlling apps is off in Settings › Model › Agent"
        return True, ""

    def offer(self, request):
        return bool(_OFFER.search(request or ""))

    def target(self, args):
        app = args.get("app")
        if app not in POLICIES:
            return ""
        return app_title(app) + (f" (window “{args['title']}”)" if args.get("title") else "")

    # ---- shared steps ----
    @staticmethod
    def _call(fn, *args):
        try:
            return fn(*args)
        except AutomationError as exc:
            raise na.ToolError(str(exc), transient=exc.transient) from None
        except OSError as exc:
            raise na.ToolError(f"Windows reported an error: {exc}") from None
        except Exception as exc:                        # COM errors from UI Automation
            raise na.ToolError(f"Windows UI Automation couldn't do that ({type(exc).__name__}: {exc}).") from None

    def find_window(self, env, ctx, app, title=None, wait=None):
        """The app's window (waiting for it to appear, e.g. right after open_app)."""
        wait = WINDOW_WAIT if wait is None else min(wait, WINDOW_WAIT)
        deadline = time.monotonic() + wait
        while True:
            windows = self._call(env.desktop.windows, app)
            if title:
                windows = [w for w in windows if title.lower() in w.title.lower()]
            if windows:
                windows.sort(key=lambda w: (not w.active, w.minimized))
                return windows[0]
            if time.monotonic() >= deadline or ctx.stopped():
                break
            time.sleep(0.25)
        named = app_title(app)
        raise na.ToolError(f"{named} isn't open" + (f" with a window titled “{title}”" if title else "")
                           + (f" (Nova waited {wait:g} s)" if wait >= 1 else "")
                           + ". Open it first" + (" (open_app)" if app in nt.APPS else "") + ".")

    def dialog_problem(self, window, dialogs):
        dialog = dialogs[0]
        buttons = ", ".join(dialog.buttons) if dialog.buttons else "no buttons Nova can read"
        return (f"{app_title(window.app)} is showing a dialog “{dialog.title or 'untitled'}” (buttons: {buttons}). "
                "Nova stopped so nothing unintended happens. Answer it yourself, or ask Nova to click one of "
                "its buttons in the dialog.")

    def ready(self, env, ctx, window, allow_dialog=False):
        """No unexpected dialog, and the window is the active one (brought to the front if needed)."""
        dialogs = self._call(env.desktop.dialogs, window)
        if dialogs and not allow_dialog:
            raise na.ToolError(self.dialog_problem(window, dialogs))
        if not window.enabled and not dialogs:
            raise na.ToolError(f"{app_title(window.app)} isn't accepting input right now (it may be busy).",
                               transient=True)
        if ctx.stopped():
            raise na.ToolError("Stopped.")
        if self._call(env.desktop.foreground) != window.handle:
            if not self._call(env.desktop.focus, window.handle):
                raise na.ToolError(f"Windows didn't let Nova bring {app_title(window.app)} to the front (its "
                                   "taskbar button may be flashing instead). Click its window once, then ask again.")
        if self._call(env.desktop.foreground) != window.handle:
            raise na.ToolError(f"{app_title(window.app)} isn't the active window, so Nova didn't continue.")
        return dialogs

    def window_output(self, window, message, **data):
        return na.Output(message, dict({"app": window.app, "title": window.title, "window": window.handle}, **data))

    # ---- Step 31: looking at the window (fresh pictures only; never a stored screenshot) ----
    @staticmethod
    def _vision(env):
        vision = getattr(env, "vision", None)
        return vision if vision is not None and env.settings.get("vision_agent_checks", True) else None

    def visual_precheck(self, env, window, control=None):
        """A fresh look at the window right before typing or clicking. A message UI Automation didn't report
        (an app's own dialog, an error banner) stops the step. Returns a note for the evidence."""
        vision = self._vision(env)
        if vision is None:
            return ""
        obs = vision.quick_check(window.handle)
        if obs.alerts:
            raise na.ToolError(f"{app_title(window.app)} seems to show a message: “{obs.alerts[0]}” (read from its "
                               f"window just now, at {obs.at()}). Nova stopped so nothing unintended happens. Deal "
                               "with it first, or ask Nova to look at the screen.")
        if not obs.ocr:
            return f"; the window couldn't be checked visually ({obs.notes[0]})" if obs.notes else ""
        note = f"; window checked on screen at {obs.at()}, no message visible"
        if control and obs.ocr and control.lower() not in obs.text().lower():
            note += f" (“{control}” isn't written on screen; it may be an icon)"
        return note

    def visual_recheck(self, env, handle):
        """(messages seen, note) from a fresh look at the window after a consequential step."""
        vision = self._vision(env)
        if vision is None or not self._call(env.desktop.alive, handle):
            return [], ""
        obs = vision.quick_check(handle)
        if not obs.ocr:
            return [], f"; the screen couldn't be re-checked ({obs.notes[0]})" if obs.notes else ""
        return obs.alerts, (f"; screen re-checked at {obs.at()}: it shows “{obs.alerts[0]}”" if obs.alerts else
                            f"; screen re-checked at {obs.at()}: no message visible")


class ListWindows(DesktopTool):
    name, title = "list_windows", "List open app windows"
    description = "List the open windows of approved apps (Notepad, Calculator, Paint, VS Code, File Explorer, " \
                  "browser), and which one is active. Read-only."
    input_schema = na.schema({"app": na.string("an app, or all", APPS + ["all"])})
    outputs = "windows (list of {app, title, active, dialog}), titles (list)"
    safety = na.READ_ONLY
    timeout = 10.0

    def prepare(self, args, env, trusted=False):
        app = args.get("app") or "all"
        return na.Prepared({"app": app}, "List open windows" + ("" if app == "all" else f" of {app_title(app)}"),
                           na.READ_ONLY)

    def execute(self, prepared, ctx, env):
        apps = APPS if prepared.params["app"] == "all" else [prepared.params["app"]]
        found = []
        for app in apps:
            for window in self._call(env.desktop.windows, app):
                dialogs = self._call(env.desktop.dialogs, window) if not window.enabled else []
                found.append({"app": app, "title": na._clean(window.title, 120), "active": window.active,
                              "dialog": dialogs[0].title if dialogs else None})
        lines = [f"{app_title(w['app'])}: “{w['title']}”" + (" (active)" if w["active"] else "")
                 + (f" — showing a dialog “{w['dialog']}”" if w["dialog"] else "") for w in found]
        return na.Output(f"{len(found)} open window(s)" + (":\n" + "\n".join(f"  • {line}" for line in lines)
                                                         if lines else "."),
                         {"windows": found, "titles": [w["title"] for w in found]})

    def verify(self, prepared, output, env):
        return True, "listed by Windows just now"

    def summary(self, output):
        return output.message.splitlines()[0]


class FocusWindow(DesktopTool):
    name, title = "focus_window", "Bring an app's window to the front"
    description = "Bring an open app's window to the front and check that it's the active window."
    input_schema = na.schema({"app": _app_arg(), "title": _title_arg()}, ["app"])
    outputs = "app, title"
    safety = na.REVERSIBLE
    timeout = WINDOW_WAIT + 10

    def prepare(self, args, env, trusted=False):
        return na.Prepared(dict(args), f"Bring {self.target(args)} to the front", na.REVERSIBLE)

    def execute(self, prepared, ctx, env):
        window = self.find_window(env, ctx, prepared.params["app"], prepared.params.get("title"))
        self.ready(env, ctx, window)
        return self.window_output(window, f"{app_title(window.app)} (“{window.title}”) is the active window.")

    def verify(self, prepared, output, env):
        ok = self._call(env.desktop.foreground) == output.data["window"]
        return ok, (f"Windows reports “{output.data['title']}” as the active window" if ok
                    else "another window became active")


class ReadWindow(DesktopTool):
    name, title = "read_window", "Read an app's window"
    description = ("Read the text (e.g. Notepad's document, Calculator's display) and the buttons and other "
                   "controls of an open app's window, through Windows UI Automation. Read-only.")
    input_schema = na.schema({"app": _app_arg(), "title": _title_arg()}, ["app"])
    outputs = "title, text, controls (list of names), dialog"
    safety = na.READ_ONLY
    timeout = 20.0

    def prepare(self, args, env, trusted=False):
        return na.Prepared(dict(args), f"Read {self.target(args)}", na.READ_ONLY)

    def execute(self, prepared, ctx, env):
        window = self.find_window(env, ctx, prepared.params["app"], prepared.params.get("title"), wait=3)
        text = self._call(env.desktop.read_text, window.handle) or ""
        controls = [c for c in self._call(env.desktop.controls, window.handle) if c.name and c.enabled]
        names = list(dict.fromkeys(f"{c.name} ({c.kind})" for c in controls if c.kind not in ("text", "pane")))[:40]
        dialogs = self._call(env.desktop.dialogs, window)
        clean = na._clean(text, 1500)
        return self.window_output(
            window, f"Read {app_title(window.app)} (“{window.title}”): {len(text):,} characters of text, "
                    f"{len(names)} control(s)" + (f"; it's showing a dialog “{dialogs[0].title}”" if dialogs else "")
                    + ".", text=clean, controls=names, dialog=dialogs[0].title if dialogs else None,
            dialog_buttons=dialogs[0].buttons if dialogs else [])

    def verify(self, prepared, output, env):
        return True, "read through Windows UI Automation just now"

    def summary(self, output):
        return output.message


class TypeText(DesktopTool):
    name, title = "type_text", "Type text into an app"
    description = ("Type text into Notepad (any text) or Calculator (digits and + - * / = ( ) .) at the cursor. "
                   "Nova brings the window to the front first and reads the text back.")
    input_schema = na.schema({"app": na.string("app", ["notepad", "calculator"]),
                              "text": na.string("the text to type", max_length=MAX_TYPE_CHARS),
                              "title": _title_arg()}, ["app", "text"])
    outputs = "app, title, characters"
    safety = na.REVERSIBLE
    timeout = 90.0
    private = frozenset({"text"})

    def prepare(self, args, env, trusted=False):
        app, text = args["app"], str(args.get("text") or "")
        policy = POLICIES.get(app)
        if policy is None or policy.type_chars is None:
            raise na.ToolError(f"Nova doesn't type into {app_title(app)}: {NO_TYPING.get(app, 'not allowed')}.")
        if not text:
            raise na.ToolError("There's no text to type.")
        bad = sorted({ch for ch in text if not re.fullmatch(policy.type_chars, ch)})
        if bad:
            shown = " ".join(repr(ch)[1:-1] for ch in bad[:8])
            raise na.ToolError(f"{app_title(app)} only accepts certain characters; these aren't allowed: {shown}")
        if nova_actions.looks_secret(text):
            raise na.ToolError("That text looks like a password, key or token. Nova never types those.")
        return na.Prepared(dict(args), f"Type {len(text):,} character{'s' if len(text) != 1 else ''} into "
                                       f"{self.target(args)}", na.REVERSIBLE, preview=nova_actions._preview(text))

    def execute(self, prepared, ctx, env):
        app, text = prepared.params["app"], prepared.params["text"]
        policy = POLICIES[app]
        window = self.find_window(env, ctx, app, prepared.params.get("title"))
        self.ready(env, ctx, window)
        focused = self._call(env.desktop.focused, window.handle)
        if policy.editor and (focused is None or not focused.editable):
            raise na.ToolError(f"No text box in {app_title(app)} has the keyboard focus, so Nova didn't type (the "
                               "text could have gone somewhere else). Click into the text, then ask again.")
        visual = self.visual_precheck(env, window)
        if self._call(env.desktop.foreground) != window.handle:          # the look took a moment: check again
            raise na.ToolError(f"{app_title(app)} isn't the active window any more, so Nova didn't type.")
        before = self._call(env.desktop.read_text, window.handle, policy.editor) or ""
        typed = self._call(env.desktop.type_text, window.handle, text, ctx.stopped)
        return self.window_output(window, f"Typed {typed:,} characters into {app_title(app)} (“{window.title}”).",
                                  characters=typed, before=len(before), _before_text=before, visual=visual)

    def verify(self, prepared, output, env):
        policy = POLICIES[output.data["app"]]
        after = self._call(env.desktop.read_text, output.data["window"], policy.editor)
        before = output.data.pop("_before_text", "")
        visual = output.data.get("visual", "")
        if after is None:
            return False, "the text box couldn't be read back"
        if policy.editor:
            normalize = (lambda s: s.replace("\r\n", "\n").replace("\r", "\n"))
            if normalize(prepared.params["text"]) in normalize(after):
                return True, "read back from the text box: it contains the typed text" + visual
            return False, "the typed text isn't in the text box"
        if after != before:
            return True, f"the app's display changed (now: “{na._clean(after, 80)}”)" + visual
        return False, "the app's display didn't change"


class PressKeys(DesktopTool):
    name, title = "press_keys", "Press a keyboard shortcut in an app"
    description = ("Press a key or shortcut in an app's window: ctrl+a, ctrl+z, ctrl+y, ctrl+n, ctrl+t, ctrl+f, "
                   "ctrl+s (save), ctrl+shift+s, ctrl+c, ctrl+x, ctrl+v, ctrl+w, enter, tab, escape, backspace, "
                   "delete, home, end, arrows. Saving, clipboard keys and closing tabs ask first.")
    input_schema = na.schema({"app": _app_arg(), "keys": na.string("the key or shortcut, e.g. ctrl+s", max_length=40),
                              "title": _title_arg()}, ["app", "keys"])
    outputs = "app, title, keys"
    safety = na.REVERSIBLE
    timeout = WINDOW_WAIT + 10

    def prepare(self, args, env, trusted=False):
        app = args["app"]
        combo = normalize_keys(args.get("keys"))
        if combo is None:
            raise na.ToolError(f"“{nt._short(args.get('keys'))}” isn't a shortcut Nova sends. Allowed: "
                               + ", ".join(sorted(KEYS)) + ".")
        if combo not in POLICIES[app].keys:
            allowed = ", ".join(sorted(POLICIES[app].keys)) or "none"
            raise na.ToolError(f"Nova doesn't send {combo} to {app_title(app)} (allowed there: {allowed}).")
        what, reason = KEYS[combo]
        return na.Prepared(dict(args, keys=combo), f"Press {combo} ({what}) in {self.target(args)}",
                           na.CONSEQUENTIAL if reason else na.REVERSIBLE, confirm=reason)

    def execute(self, prepared, ctx, env):
        window = self.find_window(env, ctx, prepared.params["app"], prepared.params.get("title"))
        self.ready(env, ctx, window)
        visual = self.visual_precheck(env, window)
        if self._call(env.desktop.foreground) != window.handle:
            raise na.ToolError(f"{app_title(window.app)} isn't the active window any more, so Nova didn't press keys.")
        self._call(env.desktop.send_keys, window.handle, prepared.params["keys"])
        time.sleep(0.3)
        return self.window_output(window, f"Pressed {prepared.params['keys']} in {app_title(window.app)}.",
                                  keys=prepared.params["keys"], visual=visual)

    def verify(self, prepared, output, env):
        handle, keys = output.data["window"], output.data["keys"]
        if keys == "ctrl+w" and not self._call(env.desktop.alive, handle):
            return True, "the window closed"
        if not self._call(env.desktop.alive, handle):
            return False, "the window is gone"
        seen, recheck = self.visual_recheck(env, handle) if prepared.confirm else ([], "")
        if keys == "ctrl+s":
            if any(FAILURE.search(text) for text in seen):
                return False, f"saving seems to have failed: the window shows “{seen[0]}”"
            title = self._call(env.desktop.title, handle)
            window = WindowInfo(handle, title, output.data["app"])
            dialogs = self._call(env.desktop.dialogs, window)
            if dialogs:
                return True, f"a dialog opened (“{dialogs[0].title}”): the document has no file yet, so Windows " \
                             "asks where to save it" + recheck
            if title.startswith("*") or title.lstrip().startswith("●"):
                return False, "the title still shows unsaved changes"
            return True, f"the title shows no unsaved changes (“{title}”)" + recheck
        return True, ("Windows accepted the keys while the window was active (Nova can't see their effect)"
                      + output.data.get("visual", "") + recheck)


class ClickControl(DesktopTool):
    name, title = "click_control", "Click a button or control in an app"
    description = ("Press a button, menu item, tab, check box… in Notepad, Calculator or Paint, found by its "
                   "name through Windows UI Automation (e.g. Calculator's “Seven”, “Plus”, “Equals”). Set in_dialog "
                   "to press a button in a dialog the app is showing (that always asks first).")
    input_schema = na.schema({"app": na.string("app", [a for a, p in POLICIES.items() if p.click]),
                              "name": na.string("the control's name as shown", max_length=120),
                              "kind": na.string("the kind of control", CONTROL_KINDS),
                              "in_dialog": na.boolean("the button is in a dialog the app is showing"),
                              "title": _title_arg()}, ["app", "name"])
    outputs = "app, title, control, method"
    safety = na.REVERSIBLE
    timeout = WINDOW_WAIT + 20

    def prepare(self, args, env, trusted=False):
        app, name = args["app"], " ".join(str(args.get("name") or "").split())
        policy = POLICIES.get(app)
        if policy is None or not policy.click:
            raise na.ToolError(f"Nova doesn't click in {app_title(app)}: {NO_CLICKING.get(app, 'not allowed')}.")
        if not name:
            raise na.ToolError("Which control? Give its name.")
        reason = None
        if args.get("in_dialog"):
            reason = f"answers a dialog in {app_title(app)} (“{name}”)"
        elif RISKY_CONTROL.search(name):
            reason = f"presses “{name}”, which may change or discard something"
        kind = args.get("kind") or "any"
        return na.Prepared(dict(args, name=name, kind=kind, in_dialog=bool(args.get("in_dialog"))),
                           f"Click “{name}”" + ("" if kind == "any" else f" ({kind})")
                           + (" in the dialog of " if args.get("in_dialog") else " in ") + self.target(args),
                           na.CONSEQUENTIAL if reason else na.REVERSIBLE, confirm=reason)

    def execute(self, prepared, ctx, env):
        params = prepared.params
        window = self.find_window(env, ctx, params["app"], params.get("title"))
        dialogs = self.ready(env, ctx, window, allow_dialog=params["in_dialog"])
        if params["in_dialog"] and not dialogs:
            raise na.ToolError(f"{app_title(window.app)} isn't showing a dialog.")
        handle = dialogs[0].handle if dialogs else window.handle
        inner = bool(dialogs) and handle == window.handle
        if dialogs and handle != window.handle:
            if self._call(env.desktop.foreground) != handle and not self._call(env.desktop.focus, handle):
                raise na.ToolError("Windows didn't let Nova bring the dialog to the front.")
        visual = "" if dialogs else self.visual_precheck(env, window, params["name"])
        result = self._call(env.desktop.click, handle, params["name"], params["kind"], inner)
        return self.window_output(window, f"Clicked “{result.control.name}” ({result.control.kind}) in "
                                          f"{app_title(window.app)}.", control=result.control.name,
                                  kind=result.control.kind, method=result.method, dialog=bool(dialogs),
                                  dialog_handle=handle if dialogs else None, visual=visual)

    def verify(self, prepared, output, env):
        how = {"invoke": "UI Automation pressed it", "toggle": "UI Automation toggled it",
               "select": "UI Automation selected it", "expand": "UI Automation opened it",
               "mouse": "Nova clicked its middle after checking that exactly that control was there"}
        evidence = f"“{output.data['control']}” was enabled and on screen; {how.get(output.data['method'], 'pressed')}"
        evidence += output.data.get("visual", "")
        if output.data.get("dialog"):
            handle = output.data["dialog_handle"]
            still = (self._call(env.desktop.alive, handle) and handle != output.data["window"]) or bool(
                self._call(env.desktop.dialogs, WindowInfo(output.data["window"], "", output.data["app"])))
            evidence += "; the dialog " + ("is still open" if still else "closed")
        if prepared.confirm:                       # a consequential click: look again before reporting it
            _seen, recheck = self.visual_recheck(env, output.data["window"])
            evidence += recheck
        return True, evidence


class CloseApp(DesktopTool):
    name, title = "close_app", "Close an app's window"
    description = ("Ask an app's window to close, like its close button. If the app asks about unsaved changes, "
                   "Nova leaves that question to the user and never discards anything. Asks first.")
    input_schema = na.schema({"app": _app_arg(), "title": _title_arg()}, ["app"])
    outputs = "app, title"
    safety = na.CONSEQUENTIAL
    timeout = WINDOW_WAIT + CLOSE_WAIT + 10

    def prepare(self, args, env, trusted=False):
        return na.Prepared(dict(args), f"Close {self.target(args)}", na.CONSEQUENTIAL,
                           confirm=f"closes {self.target(args)} (if anything is unsaved, the app asks you; Nova "
                                   "never discards anything)")

    def execute(self, prepared, ctx, env):
        window = self.find_window(env, ctx, prepared.params["app"], prepared.params.get("title"), wait=2)
        dialogs = self._call(env.desktop.dialogs, window)
        if dialogs:
            raise na.ToolError(self.dialog_problem(window, dialogs))
        self._call(env.desktop.close, window.handle)
        deadline = time.monotonic() + CLOSE_WAIT
        while time.monotonic() < deadline and not ctx.stopped():
            if not self._call(env.desktop.alive, window.handle):
                return self.window_output(window, f"Closed {app_title(window.app)} (“{window.title}”).")
            dialogs = self._call(env.desktop.dialogs, window)
            if dialogs:
                dialog = dialogs[0]
                buttons = f" ({', '.join(dialog.buttons)})" if dialog.buttons else ""
                raise na.ToolError(f"{app_title(window.app)} asked “{dialog.title or 'a question'}”{buttons} before "
                                   "closing. Nova left the question for you, so nothing was discarded.")
            time.sleep(0.2)
        raise na.ToolError(f"{app_title(window.app)} didn't close within {CLOSE_WAIT:g} seconds.")

    def verify(self, prepared, output, env):
        ok = not self._call(env.desktop.alive, output.data["window"])
        return ok, "the window is no longer open" if ok else "the window is still open"


def tools():
    return [ListWindows(), FocusWindow(), ReadWindow(), TypeText(), PressKeys(), ClickControl(), CloseApp()]


# ==================== planner rules and intent ====================
_ALIASES = {alias: key for alias, key in nova_actions.SWITCH_ALIASES.items() if key in POLICIES}
_APP = "(?:the\\s+|my\\s+)?(?P<app>" + "|".join(sorted(map(re.escape, _ALIASES), key=len, reverse=True)) + \
       ")(?:\\s+(?:app|window))?"
_QUOTED = r"(?:\"(?P<q1>[^\"]*)\"|“(?P<q2>[^”]*)”|'(?P<q3>[^']*)')"


def _quoted(match):
    for name in ("q1", "q2", "q3"):
        if match.group(name) is not None:
            return match.group(name)
    return None


def parse_clause(text, context):
    """Deterministic desktop phrases -> [(tool, args)], or None if the clause isn't one of them."""
    m = re.fullmatch(rf"(?:type|enter|write)\s+(?:{_QUOTED}|(?P<plain>.+?))(?:\s+(?:in|into|on)\s+{_APP})?", text,
                     re.I | re.S)
    if m and (m.group("app") or context.get("app") in ("notepad", "calculator")):
        app = _ALIASES[m.group("app").lower()] if m.group("app") else context["app"]
        value = _quoted(m) if _quoted(m) is not None else m.group("plain")
        if app not in ("notepad", "calculator") or value is None or (m.group("plain") and '"' in m.group("plain")) \
                or re.match(r"(?:an?\s+)?(?:new\s+)?(?:note|file|reminder)\b", value, re.I):
            return None
        context["app"] = app
        return [("type_text", {"app": app, "text": value})]
    m = re.fullmatch(rf"(?:close|quit|exit)\s+{_APP}", text, re.I)
    if m:
        app = _ALIASES[m.group("app").lower()]
        if app == "browser" and context.get("browser"):
            return None                                   # Nova's own browser window (nova_browser)
        return [("close_app", {"app": app})]
    m = re.fullmatch(rf"press\s+(?P<keys>[a-z+\- ]+?)(?:\s+(?:in|on)\s+{_APP})?", text, re.I)
    if m and normalize_keys(m.group("keys")) and (m.group("app") or context.get("app") in POLICIES):
        app = _ALIASES[m.group("app").lower()] if m.group("app") else context["app"]
        return [("press_keys", {"app": app, "keys": normalize_keys(m.group("keys"))})]
    m = re.fullmatch(rf"click\s+(?:on\s+)?(?:the\s+)?(?:{_QUOTED}|(?P<plain>[\w ]+?))(?:\s+button)?\s+(?:in|on)\s+{_APP}",
                     text, re.I)
    if m:
        app = _ALIASES[m.group("app").lower()]
        if POLICIES[app].click:
            context["app"] = app
            return [("click_control", {"app": app, "name": _quoted(m) or m.group("plain")})]
    if re.fullmatch(r"(?:list|show)\s+(?:me\s+)?(?:my\s+|the\s+|all\s+)*(?:open\s+)?windows|what\s+windows\s+are\s+open",
                    text, re.I):
        return [("list_windows", {"app": "all"})]
    m = re.fullmatch(rf"(?:read|what\s+does)\s+{_APP}(?:\s+say)?", text, re.I)
    if m:
        return [("read_window", {"app": _ALIASES[m.group("app").lower()]})]
    return None


na.CLAUSE_RULES.append(parse_clause)
na.APP_TOOLS.update({"focus_window", "read_window", "type_text", "press_keys", "click_control", "close_app"})
na.PRIVATE_ARGS["type_text"] = frozenset({"text"})
na.SOFT_REFUSALS.update({"type_text": r"type|enter", "press_keys": r"paste"})
na.INTENT.update({
    "type_text": re.compile(r"\b(?:typ\w*|write|enter|put|fill)\b", re.I),
    "press_keys": re.compile(r"\b(?:press\w*|shortcut|ctrl|control|hit|undo|redo|select\s+all|save|paste|copy|cut|"
                             r"escape|esc|enter|tab)\b", re.I),
    "click_control": re.compile(r"\b(?:click\w*|press\w*|tap|push|hit|select|choose|button|answer)\b", re.I),
    "close_app": re.compile(r"\b(?:clos\w*|quit|exit)\b", re.I),
})

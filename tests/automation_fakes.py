"""Controlled test applications for the Step 30 tests: a simulated Windows desktop and a simulated
browser that behave like nova_desktop.WindowsDesktop and nova_browser.PlaywrightBrowser (same methods,
same errors), so no real window, keystroke or web request is involved."""
import time
from dataclasses import dataclass, field, replace

import nova_browser as nb
import nova_desktop as nd


class FakeDesktop:
    def __init__(self):
        self.apps = {}                 # handle -> dict(app, title, text, controls, alive)
        self.fg = 0
        self.dialogs_ = {}             # handle -> [WindowInfo]
        self.refuse_focus = set()
        self.hidden_until = {}         # app -> number of windows() calls before its window appears
        self.focused_editable = True
        self.lose_focus_after = None   # characters typed before another window takes the focus
        self.type_delay = 0.0
        self.close_behaviour = {}      # handle -> "close" | "dialog" | "ignore"
        self.typed, self.keys, self.clicked, self.closed = [], [], [], []
        self._next = 100

    # ---- set-up ----
    def add(self, app, title, text="", controls=()):
        self._next += 1
        self.apps[self._next] = {"app": app, "title": title, "text": text, "alive": True,
                                 "controls": [replace(c) for c in controls]}
        return self._next

    def info(self, handle):
        a = self.apps[handle]
        return nd.WindowInfo(handle, a["title"], a["app"], 4242, active=self.fg == handle,
                             enabled=not self.dialogs_.get(handle))

    # ---- backend API ----
    def windows(self, app):
        if self.hidden_until.get(app, 0) > 0:
            self.hidden_until[app] -= 1
            return []
        return [self.info(h) for h, a in self.apps.items() if a["app"] == app and a["alive"]]

    def foreground(self):
        return self.fg

    def alive(self, handle):
        return handle in self.apps and self.apps[handle]["alive"]

    def title(self, handle):
        return self.apps[handle]["title"]

    def focus(self, handle):
        if handle in self.refuse_focus:
            return False
        self.fg = handle
        return True

    def close(self, handle):
        behaviour = self.close_behaviour.get(handle, "close")
        if behaviour == "close":
            self.apps[handle]["alive"] = False
            self.closed.append(handle)
        elif behaviour == "dialog":
            self.dialogs_[handle] = [nd.WindowInfo(handle, "Do you want to save changes?", self.apps[handle]["app"],
                                                   buttons=["Save", "Don't save", "Cancel"])]

    def dialogs(self, window):
        return list(self.dialogs_.get(window.handle, []))

    def controls(self, handle, limit=120, dialog=False):
        if dialog:
            return [nd.ControlInfo(b, "button") for d in self.dialogs_.get(handle, []) for b in d.buttons]
        return list(self.apps[handle]["controls"])[:limit]

    def click(self, handle, name, kind=None, dialog=False):
        if dialog:
            for d in self.dialogs_.get(handle, []):
                if name in d.buttons:
                    self.clicked.append((handle, name))
                    self.dialogs_.pop(handle, None)
                    return nd.ClickResult(nd.ControlInfo(name, "button"), "invoke")
            raise nd.AutomationError(f"There's no control called “{name}” in the window.")
        matches = [c for c in self.apps[handle]["controls"] if c.name.lower() == name.lower()
                   and (not kind or kind == "any" or c.kind == kind)]
        if not matches:
            raise nd.AutomationError(f"There's no control called “{name}” in the window.")
        if len(matches) > 1:
            raise nd.AutomationError(f"More than one control matches “{name}”.")
        control = matches[0]
        if not control.enabled or not control.on_screen:
            raise nd.AutomationError(f"“{control.name}” is disabled, so Nova didn't click it.")
        self.clicked.append((handle, control.name))
        if self.apps[handle]["app"] == "calculator":       # a tiny calculator: digits go to the display
            digits = {"One": "1", "Two": "2", "Seven": "7", "Plus": "+", "Equals": "="}
            self.apps[handle]["text"] += digits.get(control.name, "")
        return nd.ClickResult(control, "invoke")

    def focused(self, handle):
        return nd.ControlInfo("Text editor", "document", editable=self.focused_editable)

    def read_text(self, handle, focused_only=False):
        return self.apps[handle]["text"]

    def type_text(self, handle, text, should_stop):
        typed = 0
        for ch in text:
            if should_stop():
                raise nd.AutomationError(f"Stopped after typing {typed} of {len(text)} characters.")
            if self.fg != handle:
                raise nd.AutomationError(f"The window lost the focus after {typed} of {len(text)} characters, "
                                         "so Nova stopped typing.")
            self.apps[handle]["text"] += ch
            typed += 1
            self.typed.append(ch)
            if self.lose_focus_after is not None and typed >= self.lose_focus_after:
                self.fg = 0
            if self.type_delay:
                time.sleep(self.type_delay)
        return typed

    def send_keys(self, handle, combo):
        if self.fg != handle:
            raise nd.AutomationError("The window isn't active, so Nova didn't send the keys.")
        self.keys.append((handle, combo))
        if combo == "ctrl+s":
            self.apps[handle]["title"] = self.apps[handle]["title"].lstrip("*")


@dataclass
class FakePage:
    title: str
    text: str = ""
    links: dict = field(default_factory=dict)        # link text -> url
    buttons: dict = field(default_factory=dict)      # button text -> (url or None, TargetInfo kwargs)
    fields: dict = field(default_factory=dict)       # label -> kind ("text", "password", "search")
    challenge: str | None = None
    status: int = 200
    results: list = field(default_factory=list)


class FakeBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.url = None
        self.history = []
        self.values = {}
        self.ok, self.why = True, ""
        self.opened, self.clicks, self.submitted, self.aborted = [], [], [], False
        self.solve_on_intervene = None             # set by a test: the user "signs in" while Nova waits

    def available(self):
        return self.ok, self.why

    def has_page(self):
        return self.url is not None

    def reset(self):
        self.aborted = False

    def abort(self):
        self.aborted = True
        self.url = None

    def _check(self):
        if self.aborted:
            raise nb.BrowserError("The browser was stopped.")
        if self.url is None:
            raise nb.BrowserError("No web page is open in Nova's browser. Open one first.")

    def _snap(self):
        page = self.pages[self.url]
        links = [{"text": t, "url": u} for t, u in page.links.items()]
        fields = [{"label": l, "kind": k} for l, k in page.fields.items()]
        return nb.PageSnapshot(self.url, page.title, page.status, page.text, links, fields, list(page.buttons),
                               page.challenge)

    def open(self, url):
        if self.aborted:
            raise nb.BrowserError("The browser was stopped.")
        if url not in self.pages:
            raise nb.BrowserError("That site couldn't be found (check the address).")
        self.opened.append(url)
        if self.url:
            self.history.append(self.url)
        self.url = url
        return self._snap()

    def snapshot(self):
        self._check()
        return self._snap()

    def current(self):
        self._check()
        return self.url, self.pages[self.url].title

    def _target(self, target, kind):
        page = self.pages[self.url]
        if kind in ("link", "any") and target in page.links:
            return page.links[target], nb.TargetInfo(target, "a", page.links[target])
        if kind in ("button", "any") and target in page.buttons:
            url, extra = page.buttons[target]
            return url, nb.TargetInfo(target, "button", **extra)
        raise nb.BrowserError(f"There's no visible link or button called “{target}” on the page.")

    def describe(self, target, kind="any"):
        self._check()
        return self._target(target, kind)[1]

    def click(self, target, kind="any", allow_submit=False):
        self._check()
        url, info = self._target(target, kind)
        nb.check_target(info, allow_submit)
        self.clicks.append(target)
        before = self.url
        if url:
            self.history.append(self.url)
            self.url = url
        return self._snap(), self.url != before, info

    def fill(self, name, text, submit=False):
        self._check()
        page = self.pages[self.url]
        if name not in page.fields:
            raise nb.BrowserError(f"There's no field called “{name}” on the page that Nova can type into.")
        if page.fields[name] == "password":
            raise nb.BrowserError("That's a password, payment or verification-code field. Nova never types into "
                                  "those; please fill it in yourself.")
        self.values[name] = text
        before = self.url
        if submit:
            self.submitted.append((name, text))
            target = page.buttons.get("__submit__", (None, {}))[0]
            if target:
                self.history.append(self.url)
                self.url = target
        return True, self._snap(), self.url != before

    def results(self, engine):
        self._check()
        return list(self.pages[self.url].results)

    def back(self):
        self._check()
        if not self.history:
            raise nb.BrowserError("There's no earlier page to go back to.")
        self.url = self.history.pop()
        return self._snap()

    def close(self):
        self.url = None

    def stop(self):
        self.url = None

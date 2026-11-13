"""
Browser automation for Nova's agent (Step 30), with Playwright. No GUI code.

  browser_open      open a web page (http/https only) in Nova's own browser window
  browser_search    search the web with DuckDuckGo, Bing or Google (asks first: the query is sent)
  browser_read      read the open page: title, text, links, form fields, buttons (read-only)
  browser_click     click a link or button found by its visible text or accessible name
  browser_type      fill a form field (found by its label, placeholder or name); submitting asks first
  browser_back      go back one page
  browser_close     close Nova's browser window

Off by default (Settings › Model › Agent › Browser automation); needs the
optional Playwright package. Microsoft Edge or Chrome on this PC is used, so
no separate browser download is needed.

Safety model
- Nova's browser is a separate, fresh profile in its own window: none of your
  cookies, saved passwords or signed-in sessions are available to it, and it's
  discarded when closed. Downloads are refused; site permission requests
  (location, camera, notifications…) are denied automatically.
- Only http and https addresses; no file:, data:, javascript: or browser
  pages, no addresses with user names or passwords in them, and no local or
  private network addresses (your router, other PCs). Checked again after
  every navigation and redirect.
- Sign-in pages and CAPTCHAs are never filled in or solved: Nova stops and asks
  you to do it yourself in the window, then continues when you click Continue
  (or stops). Nova never types into password, payment or one-time-code fields,
  and never types text that looks like a password or key.
- Clicks are on one visible, enabled link or button found by its name;
  ambiguous names are refused. Anything that submits a form, or whose name
  suggests buying, sending, deleting, posting, subscribing or changing
  settings, asks right before. A web search asks first, showing the query.
- Page text is untrusted data: instructions planted in pages are removed
  before the model sees it, and it is never stored in the task history.
- Playwright runs on its own thread; every operation has a time limit.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import queue
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote_plus, urlsplit, urlunsplit

import nova_actions
import nova_agent as na

log = logging.getLogger("nova.browser")

NAV_TIMEOUT_MS = 20_000
ACTION_TIMEOUT_MS = 10_000
CALL_TIMEOUT = 45.0             # seconds for one browser operation, end to end
INTERVENTION_WAIT = 300         # seconds the user has to sign in or solve a check themselves
MAX_TEXT = 2000
MAX_LINKS = 20
ALLOW_PRIVATE_HOSTS = False     # tests serve pages from this PC
SEARCH_ENGINES = {
    "duckduckgo": ("DuckDuckGo", "https://html.duckduckgo.com/html/?q={q}"),
    "bing": ("Bing", "https://www.bing.com/search?q={q}"),
    "google": ("Google", "https://www.google.com/search?q={q}"),
}
CHALLENGES = {"captcha": "The page wants to check that you're human (a CAPTCHA or security check)",
              "sign_in": "The page asks you to sign in"}
_CAPTCHA_FRAME = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha", re.I)
_CAPTCHA_TEXT = re.compile(r"verify (?:that )?you(?:'re| are) (?:a )?human|i'?m not a robot|unusual traffic from your "
                           r"(?:computer|network)|are you a robot|complete the security check|press (?:and|&) hold|"
                           r"\bcaptcha\b", re.I)
_SIGN_IN = re.compile(r"(?:log|sign)[\s_-]?in|signin|login|auth|account", re.I)
SENSITIVE_FIELD = re.compile(r"pass(?:word|code|phrase)?\b|\bpin\b|card|cvv|cvc|security\s*code|expir|iban|routing|"
                             r"account\s*number|\bssn\b|social\s+security|one[\s-]*time|\botp\b|2fa|verification\s*code",
                             re.I)
RISKY_WEB = re.compile(
    r"\b(?:buy|purchase|order|checkout|check\s+out|pay|payment|subscribe|donate|send|post|publish|submit|sign\s*up|"
    r"register|delete|remove|unsubscribe|confirm|transfer|book|reserve|save\s+changes|update|change\s+password|"
    r"log\s*out|sign\s*out|deactivate|accept|agree|apply|upload|share|follow|like|reply|comment)\b", re.I)


class BrowserError(Exception):
    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


# ==================== addresses ====================
def check_url(raw):
    """A normalized http(s) address, or BrowserError explaining why Nova won't open it."""
    text = str(raw or "").strip().strip("\"'“”<>")
    if not text or len(text) > 2000 or any(ord(c) < 33 for c in text):
        raise BrowserError("That isn't a web address Nova can open.")
    if not re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise BrowserError(f"Nova only opens web pages (http and https), not “{parts.scheme}:” addresses.")
    if parts.username or parts.password:
        raise BrowserError("Nova doesn't open addresses with a user name or password in them.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BrowserError("That web address has no site name.")
    if not ALLOW_PRIVATE_HOSTS:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local
                                    or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise BrowserError("Nova doesn't open local or private network addresses (your router, other PCs).")
        if address is None and ("." not in host or host == "localhost" or
                                host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa", ".corp"))):
            raise BrowserError("Nova doesn't open local or private network addresses (your router, other PCs).")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def host_of(url):
    return (urlsplit(url).hostname or "").lower()


def same_site(a, b):
    a, b = host_of(a).removeprefix("www."), host_of(b).removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def detect_challenge(url, title, text, frames, password, status):
    if any(_CAPTCHA_FRAME.search(f or "") for f in frames):
        return "captcha"
    if len(text or "") < 3000 and (_CAPTCHA_TEXT.search(text or "") or _CAPTCHA_TEXT.search(title or "")):
        return "captcha"                                   # challenge pages are short; articles about them aren't
    if status in (401, 407):
        return "sign_in"
    if password and (_SIGN_IN.search(urlsplit(url).path or "") or _SIGN_IN.search(title or "")):
        return "sign_in"
    return None


# ==================== data ====================
@dataclass
class PageSnapshot:
    url: str
    title: str
    status: int | None = None
    text: str = ""
    links: list = field(default_factory=list)       # [{text, url}]
    fields: list = field(default_factory=list)      # [{label, kind}]
    buttons: list = field(default_factory=list)     # [text]
    challenge: str | None = None
    sign_in_form: bool = False


@dataclass
class TargetInfo:
    text: str
    tag: str
    href: str | None = None
    submit: bool = False
    method: str | None = None
    download: bool = False


# ==================== the real backend (Playwright) ====================
_SNAPSHOT_JS = r"""() => {
  const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = e => ((e.labels && e.labels.length ? e.labels[0].innerText : '') || e.getAttribute('aria-label')
    || e.placeholder || e.name || e.id || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) { if (links.length >= 60) break;
    const t = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    if (t && visible(a)) links.push({text: t.slice(0, 120), url: a.href}); }
  const fields = [];
  for (const e of document.querySelectorAll('input, textarea, select')) { if (fields.length >= 25) break;
    const type = (e.type || e.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || !visible(e)) continue;
    fields.push({label: label(e), kind: e.tagName.toLowerCase() === 'input' ? type : e.tagName.toLowerCase()}); }
  const buttons = [];
  for (const b of document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')) {
    if (buttons.length >= 30) break;
    const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    if (t && visible(b)) buttons.push(t.slice(0, 80)); }
  const password = Array.from(document.querySelectorAll('input[type=password]')).some(visible);
  return {links, fields, buttons, password, text: (document.body ? document.body.innerText : '').slice(0, 20000)};
}"""
_TARGET_JS = r"""e => { const f = e.form || e.closest('form');
  return {tag: e.tagName.toLowerCase(), type: (e.getAttribute('type') || '').toLowerCase(),
          href: e.href || e.getAttribute('href') || null, download: e.hasAttribute('download'),
          form: !!f, method: f ? (f.getAttribute('method') || 'get').toLowerCase() : null,
          text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 120)}; }"""
_FIELD_JS = r"""e => { const lab = (e.labels && e.labels.length ? e.labels[0].innerText : '') || '';
  return {tag: e.tagName.toLowerCase(), type: (e.getAttribute('type') || '').toLowerCase(),
          autocomplete: (e.getAttribute('autocomplete') || '').toLowerCase(),
          names: [e.name || '', e.id || '', lab, e.getAttribute('aria-label') || '', e.placeholder || ''].join(' ')}; }"""
_RESULTS_JS = {
    "duckduckgo": "() => Array.from(document.querySelectorAll('a.result__a')).map(a => ({title: a.innerText.trim(), url: a.href}))",
    "bing": "() => Array.from(document.querySelectorAll('li.b_algo h2 a')).map(a => ({title: a.innerText.trim(), url: a.href}))",
    "google": "() => Array.from(document.querySelectorAll('a h3')).map(h => ({title: h.innerText.trim(), url: h.closest('a').href}))",
}


class PlaywrightBrowser:
    """Nova's own browser window. Playwright's sync API belongs to one thread, so every operation runs on a
    dedicated worker thread and the caller waits (with a time limit)."""

    def __init__(self, headless=False, channels=("msedge", "chrome", None)):
        self.headless, self.channels = headless, channels
        self._jobs, self._thread, self._lock = queue.Queue(), None, threading.Lock()
        self._aborted = threading.Event()
        self._pw = self._browser = self._context = self._page = None
        self._has_page = False

    # ---- plumbing ----
    def available(self):
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False, "Playwright isn't installed (run “pip install playwright” in Nova's virtual environment)"
        return True, ""

    def has_page(self):
        return self._has_page

    def reset(self):
        """Allow operations again after an emergency stop."""
        self._aborted.clear()

    def abort(self):
        """Emergency stop: refuse further operations and close the browser as soon as the worker is free
        (an operation already running ends within its own time limit)."""
        self._aborted.set()
        self._jobs.put(("close", None, None))

    def _run(self, fn, timeout=CALL_TIMEOUT):
        if self._aborted.is_set():
            raise BrowserError("The browser was stopped.")
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="NovaBrowser")
                self._thread.start()
        box, done = {}, threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise BrowserError(f"The browser didn't respond within {timeout:g} seconds.", transient=True)
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _loop(self):
        while True:
            fn, box, done = self._jobs.get()
            if fn == "close":
                self._shutdown()
                continue
            if fn is None:
                self._shutdown()
                return
            try:
                if self._aborted.is_set():
                    raise BrowserError("The browser was stopped.")
                box["value"] = fn()
            except BrowserError as exc:
                box["error"] = exc
            except Exception as exc:
                box["error"] = self._translate(exc)
            finally:
                done.set()

    def _translate(self, exc):
        text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        if type(exc).__name__ == "TimeoutError":
            return BrowserError("The page didn't respond in time.", transient=True)
        if "ERR_NAME_NOT_RESOLVED" in text:
            return BrowserError("That site couldn't be found (check the address).")
        if "ERR_INTERNET_DISCONNECTED" in text or "ERR_NETWORK_CHANGED" in text:
            return BrowserError("This PC seems to be offline.", transient=True)
        if "has been closed" in text:
            self._page, self._has_page = None, False
            return BrowserError("The browser window was closed.")
        return BrowserError(f"The browser reported: {text[:200]}")

    def _shutdown(self):
        for item in (self._context, self._browser):
            try:
                if item is not None:
                    item.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None
        self._has_page = False

    def _ensure_page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._browser is None or not self._browser.is_connected():
            from playwright.sync_api import sync_playwright
            if self._pw is None:
                self._pw = sync_playwright().start()
            problem = None
            for channel in self.channels:
                try:
                    options = {"headless": self.headless}
                    if channel:
                        options["channel"] = channel
                    self._browser = self._pw.chromium.launch(**options)
                    break
                except Exception as exc:
                    problem = exc
            else:
                raise BrowserError("Nova couldn't start a browser: Microsoft Edge or Google Chrome is needed (or run "
                                   f"“python -m playwright install chromium”). {str(problem).splitlines()[0][:120]}")
            self._context = None
        if self._context is None:
            # A fresh profile: no cookies, saved passwords or sessions; no downloads; no permissions granted.
            self._context = self._browser.new_context(accept_downloads=False, permissions=[],
                                                      service_workers="block")
            self._context.set_default_timeout(ACTION_TIMEOUT_MS)
            self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[-1] if pages else self._context.new_page()
        self._has_page = True
        return self._page

    def _current(self):
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()] if self._context else []
            if not pages:
                self._page, self._has_page = None, False
                raise BrowserError("No web page is open in Nova's browser. Open one first.")
            self._page = pages[-1]
        return self._page

    def _snapshot(self, page, status=None):
        data = page.evaluate(_SNAPSHOT_JS)
        frames = [f.url for f in page.frames]
        title = page.title()
        challenge = detect_challenge(page.url, title, data["text"], frames, data["password"], status)
        return PageSnapshot(page.url, title, status, data["text"], data["links"], data["fields"], data["buttons"],
                            challenge, data["password"])

    def _guard(self, page):
        """After any navigation: the page must still be an allowed web address."""
        try:
            check_url(page.url)
        except BrowserError:
            if page.url.startswith("about:"):
                return
            page.goto("about:blank")
            raise BrowserError("The page went to an address Nova doesn't open (local, private or not a web page), "
                               "so Nova left it.") from None

    def _settle(self, page):
        try:
            page.wait_for_timeout(300)
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass

    # ---- operations ----
    def open(self, url):
        def job():
            page = self._ensure_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            self._guard(page)
            return self._snapshot(page, response.status if response else None)
        return self._run(job)

    def snapshot(self):
        return self._run(lambda: self._snapshot(self._current()))

    def current(self):
        def job():
            page = self._current()
            return page.url, page.title()
        return self._run(job)

    def _locate(self, page, target, kind):
        roles = {"link": ["link"], "button": ["button"]}.get(kind, ["link", "button"])
        matches = []
        for exact in (True, False):
            for role in roles:
                locator = page.get_by_role(role, name=target, exact=exact)
                for i in range(min(locator.count(), 10)):
                    if locator.nth(i).is_visible():
                        matches.append(locator.nth(i))
            if matches:
                break
        if not matches:
            raise BrowserError(f"There's no visible {kind if kind in ('link', 'button') else 'link or button'} "
                               f"called “{target}” on the page.")
        if len(matches) > 1:
            hrefs = {m.evaluate("e => e.href || null") for m in matches}
            if not (len(hrefs) == 1 and None not in hrefs):
                raise BrowserError(f"{len(matches)} links or buttons on the page match “{target}”. Use more of its "
                                   "text, so Nova clicks the right one.")
        return matches[0]

    @staticmethod
    def _target(element):
        info = element.evaluate(_TARGET_JS)
        submit = (info["tag"] == "button" and info["type"] in ("", "submit") and info["form"]) or \
                 (info["tag"] == "input" and info["type"] in ("submit", "image"))
        return TargetInfo(info["text"], info["tag"], info["href"], bool(submit), info["method"], info["download"])

    def describe(self, target, kind="any"):
        return self._run(lambda: self._target(self._locate(self._current(), target, kind)))

    def click(self, target, kind="any", allow_submit=False):
        def job():
            page = self._current()
            element = self._locate(page, target, kind)
            info = self._target(element)
            check_target(info, allow_submit)
            if not element.is_enabled():
                raise BrowserError(f"“{target}” is disabled, so Nova didn't click it.")
            before, pages = page.url, len(self._context.pages)
            element.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
            element.click(timeout=ACTION_TIMEOUT_MS)
            self._settle(page)
            if len(self._context.pages) > pages:                 # it opened a new tab: follow it
                self._page = self._context.pages[-1]
                self._settle(self._page)
            self._guard(self._page)
            snap = self._snapshot(self._page)
            return snap, snap.url != before, info
        return self._run(job)

    def _field(self, page, name):
        attribute = f"[name={json.dumps(name)}], [id={json.dumps(name)}]"
        for exact in (True, False):
            for locator in (page.get_by_label(name, exact=exact), page.get_by_placeholder(name, exact=exact),
                            page.get_by_role("textbox", name=name, exact=exact),
                            page.get_by_role("searchbox", name=name, exact=exact),
                            page.get_by_role("combobox", name=name, exact=exact)) + ((page.locator(attribute),)
                                                                                     if exact else ()):
                for i in range(min(locator.count(), 5)):
                    if locator.nth(i).is_visible() and locator.nth(i).is_editable():
                        return locator.nth(i)
        raise BrowserError(f"There's no field called “{name}” on the page that Nova can type into.")

    def fill(self, name, text, submit=False):
        def job():
            page = self._current()
            element = self._field(page, name)
            info = element.evaluate(_FIELD_JS)
            if info["type"] == "password" or re.search(r"password|cc-|one-time-code", info["autocomplete"]) or \
                    SENSITIVE_FIELD.search(info["names"]):
                raise BrowserError("That's a password, payment or verification-code field. Nova never types into "
                                   "those; please fill it in yourself.")
            if info["tag"] == "select":
                element.select_option(label=text)
                value = element.evaluate("e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''")
            else:
                element.fill(text)
                value = element.input_value()
            matches = value.strip() == text.strip()
            before = page.url
            if submit and matches:
                element.press("Enter")
                self._settle(page)
                self._guard(page)
            return matches, self._snapshot(page), page.url != before
        return self._run(job)

    def screenshot(self):
        """The visible part of the page as PNG bytes, in memory only, with password fields masked (Step 31)."""
        def job():
            page = self._current()
            return page.screenshot(type="png", mask=[page.locator("input[type=password]")], timeout=ACTION_TIMEOUT_MS)
        return self._run(job)

    def results(self, engine):
        def job():
            page = self._current()
            items = page.evaluate(_RESULTS_JS[engine]) if engine in _RESULTS_JS else []
            return [{"title": i["title"], "url": unwrap_result(i["url"])} for i in items if i.get("url")]
        return self._run(job)

    def back(self):
        def job():
            page = self._current()
            response = page.go_back(wait_until="domcontentloaded")
            if response is None and page.url in ("about:blank", ""):
                raise BrowserError("There's no earlier page to go back to.")
            self._guard(page)
            return self._snapshot(page)
        return self._run(job)

    def close(self):
        if self._thread is None or not self._thread.is_alive():
            self._has_page = False
            return
        done = threading.Event()
        self._jobs.put((lambda: self._shutdown(), {}, done))
        done.wait(10)

    def stop(self):
        """Close the browser and end the worker thread (Nova is exiting)."""
        if self._thread is not None and self._thread.is_alive():
            self._jobs.put((None, None, None))
            self._thread.join(15)                  # closing Edge and the Playwright driver can take a few seconds


def check_target(info, allow_submit):
    if info.download:
        raise BrowserError("That link downloads a file; Nova doesn't download files.")
    if info.href and re.match(r"^(?:mailto|tel|sms|file|data):", info.href, re.I):
        raise BrowserError("That link would start an email, call or message, or open a local file. Nova doesn't do "
                           "that from web pages.")
    if info.submit and not allow_submit:
        raise BrowserError("That button submits a form, so Nova stopped to ask you first. Ask again to confirm it.")


def unwrap_result(url):
    """DuckDuckGo's result links go through a redirect: the real address is in “uddg”."""
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    return url


# ==================== agent tools ====================
_OFFER = re.compile(r"\b(?:browser|web|website|site|webpage|page|url|link|online|internet|search\w*|google|bing|"
                    r"duckduckgo|wikipedia|click\w*|https?|www)\b|\.(?:com|org|net|io|dev|edu|gov|in|co|uk|ai|app)\b",
                    re.I)


class BrowserTool(na.Tool):
    retry_on_timeout = False
    timeout = CALL_TIMEOUT + INTERVENTION_WAIT + 15      # an outer bound; each operation has its own time limit

    def available(self, env):
        if env.browser is None:
            return False, "browser automation isn't available"
        if not env.settings.get("browser_automation", False):
            return False, "browser automation is off (Settings › Model › Agent)"
        return env.browser.available()

    def offer(self, request):
        return bool(_OFFER.search(request or ""))

    def target(self, args):
        return args.get("url") or "the page in Nova's browser"

    @staticmethod
    def _call(fn, *args):
        try:
            return fn(*args)
        except BrowserError as exc:
            raise na.ToolError(str(exc), transient=exc.transient) from None

    def settle(self, env, ctx, snap):
        """A sign-in page or CAPTCHA: the user does it in the window, or Nova stops. Never bypassed."""
        if not snap.challenge:
            return snap
        what = f"{CHALLENGES[snap.challenge]} on {host_of(snap.url)}"
        if env.intervene is None or ctx.stopped():
            raise na.ToolError(f"{what}. Nova never solves these or signs in for you; do it yourself in Nova's browser "
                               "window, then ask Nova to continue.")
        if not env.intervene(f"{what}. Nova never solves these, signs in or enters passwords for you. Please do it "
                             "yourself in Nova's browser window, then click Continue."):
            raise na.ToolError(f"{what}; you chose not to continue, so Nova stopped here.")
        snap = self._call(env.browser.snapshot)
        if snap.challenge:
            raise na.ToolError(f"{CHALLENGES[snap.challenge]} still. Nova stopped here.")
        return snap

    @staticmethod
    def page_data(snap, **extra):
        links = [{"text": na._clean(l["text"], 120), "url": l["url"]} for l in snap.links[:MAX_LINKS]]
        return dict({"url": snap.url, "title": na._clean(snap.title, 200), "text": na._clean(snap.text, MAX_TEXT),
                     "links": links, "urls": [l["url"] for l in links]}, **extra)


class BrowserOpen(BrowserTool):
    name, title = "browser_open", "Open a web page"
    description = ("Open a web page (an http or https address, or a site name like example.com) in Nova's own "
                   "browser window, and read its title, text and links.")
    input_schema = na.schema({"url": na.string("the web address", max_length=2000)}, ["url"])
    outputs = "url, title, text, links (list of {text, url}), urls (list of link addresses)"
    safety = na.REVERSIBLE

    def prepare(self, args, env, trusted=False):
        url = self._call(check_url, args["url"])
        host = host_of(url).removeprefix("www.")
        asked = host in (env.request or "").lower()
        return na.Prepared({"url": url}, f"Open {url} in Nova's browser",
                           na.REVERSIBLE if asked else na.CONSEQUENTIAL,
                           confirm=None if asked else f"opens a web address your request didn't name ({host})")

    def execute(self, prepared, ctx, env):
        snap = self.settle(env, ctx, self._call(env.browser.open, prepared.params["url"]))
        if snap.status and snap.status >= 400:
            raise na.ToolError(f"The site answered with error {snap.status}" + (" (not found)." if snap.status == 404
                                                                                else "."),
                               transient=snap.status >= 500)
        return na.Output(f"Opened “{na._clean(snap.title, 120) or snap.url}” ({snap.url}).", self.page_data(snap))

    def verify(self, prepared, output, env):
        url, title = self._call(env.browser.current)
        if not url or url.startswith("about:"):
            return False, "the browser isn't showing the page"
        if same_site(url, prepared.params["url"]):
            return True, f"the browser is showing “{na._clean(title, 80)}” at {url}"
        return True, f"the site redirected to {url} (“{na._clean(title, 80)}”)"

    def summary(self, output):
        return output.message


class BrowserSearch(BrowserTool):
    name, title = "browser_search", "Search the web in the browser"
    description = ("Search the web with DuckDuckGo (default), Bing or Google in Nova's browser and read the results. "
                   "Sends the query to that search engine, so the user is asked first.")
    input_schema = na.schema({"query": na.string("what to search for", max_length=300),
                              "engine": na.string("search engine", list(SEARCH_ENGINES))}, ["query"])
    outputs = "results (list of {title, url}), urls (list of result addresses), engine"
    safety = na.CONSEQUENTIAL

    def prepare(self, args, env, trusted=False):
        query = " ".join(str(args.get("query") or "").split())
        engine = args.get("engine") or "duckduckgo"
        if not query:
            raise na.ToolError("There's nothing to search for.")
        if nova_actions.looks_secret(query):
            raise na.ToolError("The search looks like it contains a password, key or token, so Nova won't send it.")
        name = SEARCH_ENGINES[engine][0]
        return na.Prepared({"query": query, "engine": engine}, f"Search {name} for “{query}”", na.CONSEQUENTIAL,
                           confirm=f"sends “{query}” to {name} (a web search in Nova's browser)")

    def execute(self, prepared, ctx, env):
        engine = prepared.params["engine"]
        name, template = SEARCH_ENGINES[engine]
        snap = self.settle(env, ctx, self._call(env.browser.open, template.format(q=quote_plus(prepared.params["query"]))))
        results = [{"title": na._clean(r["title"], 150), "url": r["url"]}
                   for r in self._call(env.browser.results, engine) if r.get("title")][:10]
        if not results:                                       # page layout changed: use its links instead
            results = [{"title": l["text"], "url": l["url"]} for l in self.page_data(snap)["links"]
                       if not same_site(l["url"], snap.url)][:10]
        listing = "\n".join(f"  {i}. {r['title']} — {r['url']}" for i, r in enumerate(results[:5], start=1))
        return na.Output(f"{len(results)} result(s) from {name} for “{prepared.params['query']}”"
                         + (":\n" + listing if listing else "."),
                         {"results": results, "urls": [r["url"] for r in results], "engine": name, "url": snap.url})

    def verify(self, prepared, output, env):
        return isinstance(output.data.get("results"), list), f"read from the results page at {output.data['url']}"

    def summary(self, output):
        return output.message.splitlines()[0]


class BrowserRead(BrowserTool):
    name, title = "browser_read", "Read the web page"
    description = "Read the page open in Nova's browser: its title, text, links, form fields and buttons. Read-only."
    input_schema = na.schema({})
    outputs = "url, title, text, links (list of {text, url}), urls, fields (list of {label, kind}), buttons (list)"
    safety = na.READ_ONLY
    timeout = CALL_TIMEOUT + INTERVENTION_WAIT + 15

    def prepare(self, args, env, trusted=False):
        return na.Prepared({}, "Read the page in Nova's browser", na.READ_ONLY)

    def execute(self, prepared, ctx, env):
        if not env.browser.has_page():
            raise na.ToolError("No web page is open in Nova's browser. Open one first (browser_open).")
        snap = self.settle(env, ctx, self._call(env.browser.snapshot))
        fields = [{"label": na._clean(f["label"], 80), "kind": f["kind"]} for f in snap.fields]
        buttons = [na._clean(b, 80) for b in snap.buttons]
        data = self.page_data(snap, fields=fields, buttons=buttons)
        return na.Output(f"Read “{data['title'] or snap.url}” ({snap.url}): {len(snap.text):,} characters, "
                         f"{len(data['links'])} link(s), {len(fields)} field(s), {len(buttons)} button(s).", data)

    def verify(self, prepared, output, env):
        return True, "read from the page just now"


class BrowserClick(BrowserTool):
    name, title = "browser_click", "Click a link or button on the web page"
    description = ("Click one link or button on the page in Nova's browser, found by its visible text. Buttons that "
                   "submit a form or buy, send, delete, post or change something ask first.")
    input_schema = na.schema({"target": na.string("the link's or button's text", max_length=200),
                              "kind": na.string("what it is", ["link", "button", "any"])}, ["target"])
    outputs = "url, title, text, links, urls, navigated"
    safety = na.REVERSIBLE

    def prepare(self, args, env, trusted=False):
        target, kind = " ".join(str(args.get("target") or "").split()), args.get("kind") or "any"
        if not target:
            raise na.ToolError("Which link or button? Give its text.")
        reason = None
        if RISKY_WEB.search(target):
            reason = f"clicks “{target}”, which may send, buy, delete or change something"
        if env.browser is not None and env.browser.has_page():
            info = self._call(env.browser.describe, target, kind)       # the live page, right before running
            self._call(check_target, info, True)
            if info.submit and not reason:
                reason = "submits a form on the page" + (" (it sends data to the website)" if info.method == "post"
                                                         else "")
        return na.Prepared({"target": target, "kind": kind, "allow_submit": bool(reason)},
                           f"Click “{target}” on the web page", na.CONSEQUENTIAL if reason else na.REVERSIBLE,
                           confirm=reason)

    def execute(self, prepared, ctx, env):
        p = prepared.params
        snap, navigated, info = self._call(env.browser.click, p["target"], p["kind"], p["allow_submit"])
        snap = self.settle(env, ctx, snap)
        return na.Output(f"Clicked “{info.text or p['target']}”" + (f"; the page is now “{na._clean(snap.title, 100)}” "
                                                                     f"({snap.url})." if navigated else
                                                                     f"; the page stayed at {snap.url}."),
                         self.page_data(snap, navigated=navigated))

    def verify(self, prepared, output, env):
        url, _title = self._call(env.browser.current)
        if output.data["navigated"]:
            return True, f"the link or button was visible and enabled; the page changed to {url}"
        return True, f"the link or button was visible and enabled and was clicked; the page stayed at {url}"


class BrowserType(BrowserTool):
    name, title = "browser_type", "Type into a field on the web page"
    description = ("Fill a field on the page in Nova's browser (found by its label, placeholder or name). Set submit "
                   "to press Enter afterwards (that asks first). Never password, payment or code fields.")
    input_schema = na.schema({"field": na.string("the field's label, placeholder or name", max_length=120),
                              "text": na.string("the text to enter", max_length=2000),
                              "submit": na.boolean("press Enter afterwards to submit")}, ["field", "text"])
    outputs = "url, title, field, submitted"
    safety = na.REVERSIBLE
    private = frozenset({"text"})

    def prepare(self, args, env, trusted=False):
        name, text = " ".join(str(args.get("field") or "").split()), str(args.get("text") or "")
        if not name or not text.strip():
            raise na.ToolError("Give the field and the text to type.")
        if SENSITIVE_FIELD.search(name):
            raise na.ToolError("Nova never types into password, payment or verification-code fields; please fill "
                               "that in yourself.")
        if nova_actions.looks_secret(text):
            raise na.ToolError("That text looks like a password, key or token. Nova never types those.")
        submit = bool(args.get("submit"))
        return na.Prepared({"field": name, "text": text, "submit": submit},
                           f"Type {len(text):,} characters into “{name}” on the web page"
                           + (" and submit it" if submit else ""), na.CONSEQUENTIAL if submit else na.REVERSIBLE,
                           confirm="presses Enter to submit what's typed to the website" if submit else None,
                           preview=nova_actions._preview(text))

    def execute(self, prepared, ctx, env):
        p = prepared.params
        matches, snap, navigated = self._call(env.browser.fill, p["field"], p["text"], p["submit"])
        if not matches:
            raise na.ToolError(f"The field “{p['field']}” didn't take the text as typed, so Nova didn't submit it.")
        if p["submit"]:
            snap = self.settle(env, ctx, snap)
        return na.Output(f"Typed {len(p['text']):,} characters into “{p['field']}”"
                         + (f" and submitted it; the page is now {snap.url}." if p["submit"] else "."),
                         self.page_data(snap, field=p["field"], submitted=p["submit"], navigated=navigated,
                                        matched=matches))

    def verify(self, prepared, output, env):
        evidence = "read back from the field: it held the typed text"
        if output.data["submitted"]:
            evidence += f"; after submitting, the page showed {output.data['url']}"
        return bool(output.data.get("matched")), evidence


class BrowserBack(BrowserTool):
    name, title = "browser_back", "Go back to the previous web page"
    description = "Go back one page in Nova's browser."
    input_schema = na.schema({})
    outputs = "url, title, text, links, urls"
    safety = na.REVERSIBLE

    def prepare(self, args, env, trusted=False):
        return na.Prepared({}, "Go back one page in Nova's browser", na.REVERSIBLE)

    def execute(self, prepared, ctx, env):
        snap = self._call(env.browser.back)
        return na.Output(f"Went back to “{na._clean(snap.title, 100)}” ({snap.url}).", self.page_data(snap))

    def verify(self, prepared, output, env):
        url, _title = self._call(env.browser.current)
        return url == output.data["url"], f"the browser is showing {url}"


class BrowserClose(BrowserTool):
    name, title = "browser_close", "Close Nova's browser"
    description = "Close Nova's browser window (its fresh profile is discarded)."
    input_schema = na.schema({})
    outputs = ""
    safety = na.REVERSIBLE

    def prepare(self, args, env, trusted=False):
        return na.Prepared({}, "Close Nova's browser window", na.REVERSIBLE)

    def execute(self, prepared, ctx, env):
        self._call(env.browser.close)
        return na.Output("Closed Nova's browser window.")

    def verify(self, prepared, output, env):
        ok = not env.browser.has_page()
        return ok, "no page is open any more" if ok else "the browser is still open"


def tools():
    return [BrowserOpen(), BrowserSearch(), BrowserRead(), BrowserClick(), BrowserType(), BrowserBack(),
            BrowserClose()]


# ==================== planner rules and intent ====================
_URL = (r"(?P<url>https?://\S+|www\.\S+|[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|edu|gov|info|co|uk|in|de|fr|ai|"
        r"app|wiki|me|tv)(?:/\S*)?)")
_QUOTED = r"(?:\"(?P<q1>[^\"]*)\"|“(?P<q2>[^”]*)”|'(?P<q3>[^']*)')"


def _quoted(match):
    return next((match.group(n) for n in ("q1", "q2", "q3") if match.group(n) is not None), None)


def parse_clause(text, context):
    """Deterministic browser phrases -> [(tool, args)], or None if the clause isn't one of them."""
    m = re.fullmatch(rf"(?:open|go\s+to|visit|browse\s+to|navigate\s+to|load)\s+(?:the\s+)?(?:website\s+|site\s+|page\s+)?"
                     rf"{_URL}(?:\s+in\s+(?:the\s+|my\s+|nova'?s\s+)?browser)?", text, re.I)
    if m:
        context["browser"] = True
        return [("browser_open", {"url": m.group("url").rstrip(".,;")})]
    m = (re.fullmatch(r"search\s+(?:the\s+web|online|the\s+internet|(?:on\s+)?(?P<engine>duckduckgo|bing|google))\s+"
                      r"for\s+(?P<q>.+)", text, re.I)
         or re.fullmatch(r"search\s+(?:for\s+)?(?P<q>.+?)\s+(?:on|in|with)\s+(?:the\s+web|the\s+browser|"
                         r"(?P<engine>duckduckgo|bing|google))", text, re.I))
    if m:
        context["browser"] = True
        return [("browser_search", {"query": m.group("q").strip().strip("\"'“”"),
                                    "engine": (m.group("engine") or "duckduckgo").lower()})]
    if not context.get("browser"):
        return None
    m = re.fullmatch(rf"click\s+(?:on\s+)?(?:the\s+)?(?:{_QUOTED}|(?P<plain>.+?))(?:\s+(?P<kind>link|button))?", text,
                     re.I)
    if m:
        return [("browser_click", {"target": _quoted(m) or m.group("plain"), "kind": (m.group("kind") or "any").lower()})]
    m = re.fullmatch(rf"(?:type|enter)\s+{_QUOTED}\s+(?:in|into)\s+(?:the\s+)?(?P<field>.+?)(?:\s+(?:field|box))?"
                     r"(?P<submit>\s+and\s+(?:submit(?:\s+it)?|press\s+enter|search))?", text, re.I)
    if m:
        return [("browser_type", {"field": m.group("field"), "text": _quoted(m), "submit": bool(m.group("submit"))})]
    if re.fullmatch(r"(?:read|summari[sz]e|show\s+me)\s+(?:the\s+|this\s+)?(?:web\s*)?page", text, re.I):
        return [("browser_read", {})]
    if re.fullmatch(r"go\s+back(?:\s+(?:a|one)\s+page)?", text, re.I):
        return [("browser_back", {})]
    if re.fullmatch(r"(?:close|quit)\s+(?:the\s+|nova'?s\s+)?browser", text, re.I):
        context["browser"] = False
        return [("browser_close", {})]
    return None


na.CLAUSE_RULES.append(parse_clause)
na.PRIVATE_ARGS["browser_type"] = frozenset({"text"})
na.SOFT_REFUSALS["browser_type"] = r"type|enter"
na.ARG_GUARDS["browser_type"] = [("submit", re.compile(r"\b(?:submit\w*|search\w*|send|press\s+enter|go|look\s+up|"
                                                       r"find)\b", re.I),
                                  "Nova won't submit the form: the request didn't ask for that.")]
na.INTENT.update({
    "browser_open": re.compile(r"\b(?:open\w*|go|visit\w*|brows\w*|navigat\w*|load|result|link|page|site|website|url)\b"
                               r"|https?://|www\.", re.I),
    "browser_search": re.compile(r"\b(?:search\w*|look\s+up|google|bing|duckduckgo|find)\b", re.I),
    "browser_click": re.compile(r"\b(?:click\w*|press\w*|tap|follow|open\w*|select|choose)\b", re.I),
    "browser_type": re.compile(r"\b(?:typ\w*|enter|fill|write|search\w*|put)\b", re.I),
    "browser_back": re.compile(r"\b(?:back|previous)\b", re.I),
    "browser_close": re.compile(r"\b(?:clos\w*|quit|exit)\b", re.I),
})

"""Theme and reusable widgets for the Nova desktop app (CustomTkinter)."""
from __future__ import annotations

import re
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime, timedelta

import customtkinter as ctk


# -------------------- Theme --------------------
BG = "#1b1c20"            # main chat area
SIDEBAR = "#131417"
SURFACE = "#24252a"       # cards, composer
SURFACE_2 = "#2e2f35"     # hover, inputs
BORDER = "#34353c"
TEXT = "#ececf1"
MUTED = "#a1a2ab"
SUBTLE = "#6f7079"
ACCENT = "#7c8cff"
ACCENT_HOVER = "#6877f2"
ACCENT_SOFT = "#2b2f4d"
USER_BUBBLE = "#2c2f3d"
CODE_BG = "#111215"
CODE_HEAD = "#1a1b1f"
INLINE_CODE_FG = "#f2b880"
SELECTION = "#3a4683"
FOUND_TERM_BG = "#6b5a17"  # search-term highlight (amber, distinct from selection)
SUCCESS = "#4ade80"
WARNING = "#fbbf24"
DANGER = "#f87171"
DANGER_SOFT = "#3b2226"

CHAT_MAX_WIDTH = 780      # reading column width in the chat view


class Icon:
    """Segoe Fluent Icons / Segoe MDL2 Assets code points."""
    SEARCH = ""
    DOCUMENT = ""
    FOLDER = ""
    REFRESH = ""
    CLOSE = ""
    PIN = ""
    UNPIN = ""
    EDIT = ""
    VIEW = ""
    HIDE = ""
    TASKS = ""
    STOP = ""
    ADD = ""
    DELETE = ""
    SETTINGS = ""
    MIC = ""
    SEND = ""
    NOTE = ""
    MEMORY = ""
    CHECK = ""
    CLOCK = ""
    INFO = ""
    WARNING = ""


class Fonts:
    """Font families picked from what's installed (Windows 11 → Segoe UI Variable)."""

    def __init__(self, root):
        families = set(tkfont.families(root))

        def pick(*names):
            return next((n for n in names if n in families), names[-1])

        self.text = pick("Segoe UI Variable Text", "Segoe UI", "Arial")
        self.text_semibold = pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold", self.text)
        self.display = pick("Segoe UI Variable Display Semib", "Segoe UI Semibold", self.text)
        self.mono = pick("Cascadia Mono", "Consolas", "Courier New")
        self.icons = pick("Segoe Fluent Icons", "Segoe MDL2 Assets", self.text)
        self.symbol = pick("Segoe UI Symbol", self.text)

    # CustomTkinter fonts (sizes in pixels, DPI-scaled by CustomTkinter)
    def ui(self, size=14, semibold=False):
        return ctk.CTkFont(family=self.text_semibold if semibold else self.text, size=size)

    def heading(self, size=20):
        return ctk.CTkFont(family=self.display, size=size)

    def icon(self, size=16):
        return ctk.CTkFont(family=self.icons, size=size)

    # Plain Tk font tuples for tk.Text tags (negative size = pixels)
    def tk(self, size=15, semibold=False, *styles):
        return (self.text_semibold if semibold else self.text, -size, *styles)

    def tk_mono(self, size=13):
        return (self.mono, -size)


# -------------------- Small helpers --------------------
def fit_text(text, font, max_px):
    """Shorten text with an ellipsis so it fits in max_px."""
    text = " ".join(str(text).split())
    if font.measure(text) <= max_px:
        return text
    while text and font.measure(text + "…") > max_px:
        text = text[:-1]
    return text.rstrip() + "…"


def date_group(stamp):
    """Sidebar group name for a timestamp: Today, Yesterday, …"""
    try:
        parsed = datetime.fromisoformat(str(stamp))
        day = (parsed.astimezone() if parsed.tzinfo else parsed).date()
    except (TypeError, ValueError):
        return "Older"
    today = date.today()
    if day >= today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day > today - timedelta(days=7):
        return "Previous 7 days"
    if day > today - timedelta(days=30):
        return "Previous 30 days"
    return "Older"


def pointer_inside(widget):
    """True if the mouse pointer is over widget or one of its descendants."""
    try:
        x, y = widget.winfo_pointerxy()
        under = widget.winfo_containing(x, y)
    except (tk.TclError, KeyError):
        return False
    if under is None:
        return False
    path, own = str(under), str(widget)
    return path == own or path.startswith(own + ".")


def bind_tree(widget, sequence, callback):
    """Bind an event on a widget and all its descendants.

    A CustomTkinter widget's own bind() already covers its internal canvas and
    labels, so for those we only recurse into child CTk widgets; recursing
    into the internals too would fire the callback twice.
    """
    try:
        widget.bind(sequence, callback, add="+")
    except (ValueError, NotImplementedError, tk.TclError):
        pass
    is_ctk = isinstance(widget, ctk.CTkBaseClass)
    for child in widget.winfo_children():
        if not is_ctk or isinstance(child, ctk.CTkBaseClass):
            bind_tree(child, sequence, callback)


def autohide_scrollbar(scrollable):
    """Show a CTkScrollableFrame's scrollbar only when its content overflows."""
    canvas = getattr(scrollable, "_parent_canvas", None)
    bar = getattr(scrollable, "_scrollbar", None)
    if canvas is None or bar is None:
        return

    def on_scroll(first, last):
        bar.set(first, last)
        try:
            if float(first) <= 0.0 and float(last) >= 1.0:
                bar.grid_remove()
            else:
                bar.grid()
        except tk.TclError:
            pass

    canvas.configure(yscrollcommand=on_scroll)


def set_cursor(widget, cursor="hand2"):
    """Set the mouse cursor on a widget and every internal part of it."""
    try:
        tk.Misc.configure(widget, cursor=cursor)
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        set_cursor(child, cursor)


def icon_button(master, fonts, glyph, command, size=34, icon_size=16, fg="transparent",
                hover=SURFACE_2, text_color=MUTED, **kwargs):
    return ctk.CTkButton(
        master, text=glyph, font=fonts.icon(icon_size), width=size, height=size,
        corner_radius=size // 2, fg_color=fg, hover_color=hover,
        text_color=text_color, command=command, **kwargs,
    )


class Tooltip:
    """Small delayed tooltip for icon-only buttons."""

    def __init__(self, widget, text, fonts, delay=450):
        self.widget, self.text, self.fonts, self.delay = widget, text, fonts, delay
        self.tip = None
        self.after_id = None
        bind_tree(widget, "<Enter>", self._schedule)
        bind_tree(widget, "<Leave>", self._hide)
        bind_tree(widget, "<ButtonPress>", self._hide)

    def _schedule(self, _event=None):
        self._cancel()
        self.after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def _show(self):
        if self.tip or not self.widget.winfo_ismapped():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() - 34
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        label = tk.Label(
            self.tip, text=self.text, bg=SURFACE_2, fg=TEXT, padx=9, pady=4,
            font=self.fonts.tk(12), highlightthickness=1, highlightbackground=BORDER,
        )
        label.pack()
        self.tip.update_idletasks()
        self.tip.geometry(f"+{x - self.tip.winfo_width() // 2}+{y}")

    def _hide(self, _event=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class NavButton(ctk.CTkFrame):
    """Sidebar row: icon + label, highlighted on hover."""

    def __init__(self, master, fonts, glyph, label, command, filled=False):
        self._base = SURFACE if filled else "transparent"
        super().__init__(master, fg_color=self._base, corner_radius=10, height=40)
        self._command = command
        self.icon = ctk.CTkLabel(self, text=glyph, font=fonts.icon(15), text_color=TEXT if filled else MUTED, width=22)
        self.icon.pack(side="left", padx=(12, 8), pady=8)
        self.label = ctk.CTkLabel(self, text=label, font=fonts.ui(14, semibold=filled), text_color=TEXT, anchor="w")
        self.label.pack(side="left", fill="x", expand=True, padx=(0, 10))
        bind_tree(self, "<Enter>", lambda _e: self.configure(fg_color=SURFACE_2))
        bind_tree(self, "<Leave>", self._leave)
        bind_tree(self, "<Button-1>", lambda _e: self._command())
        set_cursor(self)

    def _leave(self, _event=None):
        if not pointer_inside(self):
            self.configure(fg_color=self._base)


class StatusPill(ctk.CTkFrame):
    """Rounded status indicator: coloured dot + text."""

    def __init__(self, master, fonts, textvariable):
        super().__init__(master, fg_color=SURFACE, corner_radius=14, height=28)
        self.dot = ctk.CTkLabel(self, text="●", font=ctk.CTkFont(family=fonts.symbol, size=11), text_color=SUCCESS, width=10)
        self.dot.pack(side="left", padx=(11, 6), pady=3)
        ctk.CTkLabel(self, textvariable=textvariable, font=fonts.ui(13), text_color=MUTED).pack(side="left", padx=(0, 12))

    def set_color(self, color):
        self.dot.configure(text_color=color)


# -------------------- Conversation list --------------------
class _SessionRow(ctk.CTkFrame):
    """One sidebar chat. The delete button is only created when first needed
    (hover or selection): most rows never need it, and widgets are costly."""

    def __init__(self, owner, master, fonts, session_id, title):
        super().__init__(master, fg_color="transparent", corner_radius=9, height=36)
        self.owner, self.session_id, self.selected, self.fonts = owner, session_id, False, fonts
        self.pack_propagate(False)
        self.title = None
        self.label = ctk.CTkLabel(self, text="", font=fonts.ui(14), text_color=MUTED, anchor="w")
        self.label.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.set_title(title)
        self.delete_button = None
        # Bind the row and its label only (not the delete button, whose click
        # must not also select the chat). CTk's bind() covers each widget's internals.
        for widget in (self, self.label):
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
            widget.bind("<Button-1>", lambda _e: owner.on_select(session_id), add="+")
            widget.bind("<Button-3>", lambda e: owner.show_menu(e, session_id), add="+")
        set_cursor(self)

    def set_title(self, title):
        if title != self.title:
            self.title = title
            self.label.configure(text=fit_text(title, self.owner.row_font, 188))

    def _show_delete(self):
        if self.delete_button is None:
            self.delete_button = icon_button(
                self, self.fonts, Icon.DELETE, lambda: self.owner.on_delete(self.session_id),
                size=26, icon_size=12, hover=DANGER_SOFT, text_color=SUBTLE,
            )
            Tooltip(self.delete_button, "Delete chat", self.fonts)
            self.delete_button.bind("<Leave>", self._leave, add="+")
        self.delete_button.pack(side="right", padx=(2, 5), before=self.label)

    def _hide_delete(self):
        if self.delete_button is not None:
            self.delete_button.pack_forget()

    def _enter(self, _event=None):
        if not self.selected:
            self.configure(fg_color=SURFACE)
        self._show_delete()

    def _leave(self, _event=None):
        if pointer_inside(self):
            return
        if not self.selected:
            self.configure(fg_color="transparent")
            self._hide_delete()

    def set_selected(self, selected):
        if selected == self.selected:
            return
        self.selected = selected
        self.configure(fg_color=SURFACE_2 if selected else "transparent")
        self.label.configure(text_color=TEXT if selected else MUTED)
        if selected:
            self._show_delete()
        else:
            self._hide_delete()


class SessionList(ctk.CTkScrollableFrame):
    """Sidebar conversation list grouped by date (Today, Yesterday, …).

    Rows are reused between refreshes (only new chats get new widgets, only
    changed titles are redrawn) and chats are shown PAGE at a time, so the
    list stays quick with hundreds of conversations.
    """

    PAGE = 60

    def __init__(self, master, fonts, on_select, on_delete, **kwargs):
        super().__init__(
            master, fg_color=SIDEBAR, corner_radius=0,
            scrollbar_button_color=SIDEBAR, scrollbar_button_hover_color=SURFACE_2, **kwargs,
        )
        self.fonts, self.on_select, self.on_delete = fonts, on_select, on_delete
        self.on_menu = None                          # on_menu(menu, session_id) adds right-click entries
        self.row_font = fonts.ui(14)
        self.rows = {}
        self.headers = {}
        self.limit = self.PAGE
        self._sessions, self._selected, self._layout = [], None, None
        self.more = None
        self.menu = tk.Menu(self, tearoff=False)
        autohide_scrollbar(self)

    def set_sessions(self, sessions, selected=None):
        self._sessions, self._selected = list(sessions), selected
        ids = [s[0] for s in self._sessions]
        if selected in ids and ids.index(selected) >= self.limit:
            self.limit = (ids.index(selected) // self.PAGE + 1) * self.PAGE   # keep the open chat visible
        visible = self._sessions[:self.limit]
        wanted = {s[0] for s in visible}
        for session_id in [sid for sid in self.rows if sid not in wanted]:
            self.rows.pop(session_id).destroy()
        layout = []
        for session_id, title, stamp in visible:
            row = self.rows.get(session_id)
            if row is None:
                row = self.rows[session_id] = _SessionRow(self, self, self.fonts, session_id, title)
            else:
                row.set_title(title)
            layout.append((date_group(stamp), session_id))
        hidden = len(self._sessions) - len(visible)
        if (layout, hidden) != self._layout:   # re-pack only when order, groups or paging changed
            self._repack(layout, hidden)
            self._layout = (layout, hidden)
        self.set_selected(selected)

    def _repack(self, layout, hidden):
        for child in self.winfo_children():
            child.pack_forget()
        groups = {group for group, _sid in layout}
        for name in [g for g in self.headers if g not in groups]:
            self.headers.pop(name).destroy()
        current = None
        for index, (group, session_id) in enumerate(layout):
            if group != current:
                current = group
                header = self.headers.get(group)
                if header is None:
                    header = self.headers[group] = ctk.CTkLabel(
                        self, text=group, font=self.fonts.ui(12, semibold=True), text_color=SUBTLE, anchor="w")
                header.pack(fill="x", padx=12, pady=(14 if index else 4, 4))
            self.rows[session_id].pack(fill="x", padx=(4, 6), pady=1)
        if hidden > 0:
            if self.more is None:
                self.more = ctk.CTkButton(
                    self, text="", height=30, corner_radius=9, fg_color="transparent", hover_color=SURFACE,
                    text_color=SUBTLE, font=self.fonts.ui(13), command=self._show_more)
            self.more.configure(text=f"Show more chats ({hidden})")
            self.more.pack(fill="x", padx=(4, 6), pady=(8, 4))

    def _show_more(self):
        self.limit += self.PAGE
        self.set_sessions(self._sessions, self._selected)

    def set_selected(self, session_id):
        for sid, row in self.rows.items():
            row.set_selected(sid == session_id)

    def show_menu(self, event, session_id):
        self.menu.delete(0, "end")
        if self.on_menu is not None:
            self.on_menu(self.menu, session_id)    # e.g. "Move to project"
        self.menu.add_command(label="Delete chat", command=lambda: self.on_delete(session_id))
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


# -------------------- Chat view --------------------
def open_web_link(url):
    """Open a clicked http(s) link in the default browser (nothing else is ever opened)."""
    import webbrowser
    from urllib.parse import urlsplit
    if urlsplit(url).scheme in ("http", "https"):
        webbrowser.open(url)


_INLINE = re.compile(
    # Plain web addresses become clickable, always showing the full address. [text](url)
    # links aren't rendered, so a web page can't disguise where a link goes.
    r"(?P<url>https?://(?:[^\s<>()\[\]\"'`]|\([^\s<>()\[\]\"'`]*\))+(?<![.,;:!?*]))"
    r"|\*\*\*(?P<bi>.+?)\*\*\*"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|`(?P<code>[^`\n]+)`"
    r"|(?<![\w*])\*(?P<i>[^*\s](?:[^*\n]*[^*\s])?)\*(?![\w*])"
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")


class ChatView(ctk.CTkFrame):
    """Scrollable conversation: user bubbles, Markdown-rendered replies, info lines.

    Nova's replies live in a tk.Text so they can be selected and copied; the
    text is kept in a centred reading column of at most CHAT_MAX_WIDTH pixels.
    """

    def __init__(self, master, fonts, on_suggestion=None):
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.fonts = fonts
        self.on_suggestion = on_suggestion
        self.bubbles = []          # embedded user-message labels (for re-wrapping)
        self.welcome_visible = False
        self._stream_text = ""
        self._streaming = False
        self._column_width = CHAT_MAX_WIDTH
        self._scroll_needed = False
        self._message_marks = []   # text marks at the start of each loaded message
        self._flash_job = None
        self._flashed = []         # user bubbles currently highlighted
        self._actions = []         # embedded buttons ("Show earlier messages")
        self._links = 0            # clickable "what Nova used" lines (tags context_link_N)
        self._bulk = False         # loading many messages: scroll once at the end

        self.text = tk.Text(
            self, wrap="word", bg=BG, fg=TEXT, relief="flat", borderwidth=0,
            highlightthickness=0, padx=0, pady=18, cursor="arrow",
            font=fonts.tk(15), spacing1=1, spacing3=1,
            selectbackground=SELECTION, selectforeground=TEXT,
            inactiveselectbackground=SELECTION, insertwidth=0, takefocus=0,
        )
        self.scrollbar = ctk.CTkScrollbar(
            self, command=self.text.yview, fg_color=BG,
            button_color=SURFACE_2, button_hover_color=BORDER, width=12,
        )
        self.text.configure(yscrollcommand=self._on_scroll, state="disabled")
        self._configure_tags()
        self.bind("<Configure>", self._layout)
        # Scroll the conversation from anywhere in the chat area, not only over text.
        self.bind("<MouseWheel>", self._wheel)
        self.text.bind("<Button-3>", self._context_menu)
        self.menu = tk.Menu(self, tearoff=False)

    # ---- layout ----
    def _layout(self, event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 50:
            return
        bar = 14
        column = max(280, min(CHAT_MAX_WIDTH, width - 2 * 28 - bar))
        self._column_width = column
        self.text.place(x=(width - bar - column) // 2, y=0, width=column, height=height)
        # CTk widgets take their size from configure(), not place().
        self.scrollbar.configure(height=max(20, height - 8))
        self._place_scrollbar()
        for label in self.bubbles:
            label.configure(wraplength=int(column * 0.72))

    def _place_scrollbar(self):
        if self._scroll_needed:
            self.scrollbar.place(x=self.winfo_width() - 14, y=4)
        else:
            self.scrollbar.place_forget()

    def _on_scroll(self, first, last):
        """Text yscrollcommand: update the scrollbar and hide it when everything fits."""
        self.scrollbar.set(first, last)
        needed = not (float(first) <= 0.0 and float(last) >= 1.0)
        if needed != self._scroll_needed:
            self._scroll_needed = needed
            self._place_scrollbar()

    def column_width(self):
        return self._column_width

    def _wheel(self, event):
        self.text.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _configure_tags(self):
        f, t = self.fonts, self.text
        t.tag_configure("body", font=f.tk(15), foreground=TEXT, lmargin1=2, lmargin2=2, rmargin=4, spacing1=3, spacing3=3)
        t.tag_configure("bold", font=f.tk(15, True))
        t.tag_configure("italic", font=f.tk(15, False, "italic"))
        t.tag_configure("bold_italic", font=f.tk(15, True, "italic"))
        t.tag_configure("inline_code", font=f.tk_mono(13), foreground=INLINE_CODE_FG, background=SURFACE)
        t.tag_configure("h1", font=(f.display, -22), spacing1=14, spacing3=6)
        t.tag_configure("h2", font=(f.display, -19), spacing1=12, spacing3=5)
        t.tag_configure("h3", font=f.tk(16, True), spacing1=10, spacing3=4)
        for level in range(3):
            indent = 6 + level * 20
            t.tag_configure(f"li{level}", lmargin1=indent, lmargin2=indent + 20, spacing1=2, spacing3=2)
        t.tag_configure("bullet_mark", foreground=ACCENT)
        t.tag_configure("quote", foreground=MUTED, lmargin1=16, lmargin2=16, font=f.tk(15, False, "italic"))
        t.tag_configure("table", font=f.tk_mono(13), foreground=TEXT, lmargin1=6, lmargin2=6)
        code_common = dict(background=CODE_BG, lmargin1=16, lmargin2=16, rmargin=16,
                           lmargincolor=CODE_BG, rmargincolor=CODE_BG)
        t.tag_configure("code", font=f.tk_mono(13), foreground="#d7dae0", spacing1=1, spacing3=1, **code_common)
        t.tag_configure("code_head", font=f.tk_mono(11), foreground=SUBTLE, spacing1=6, spacing3=6,
                        **{**code_common, "background": CODE_HEAD, "lmargincolor": CODE_HEAD, "rmargincolor": CODE_HEAD})
        t.tag_configure("code_pad", font=(f.mono, -6), **code_common)
        t.tag_configure("gap", font=(f.text, -7))
        t.tag_configure("msg_gap", font=(f.text, -14))
        t.tag_configure("role", font=f.tk(13, True), foreground=MUTED, spacing1=6, spacing3=4, lmargin1=2)
        t.tag_configure("role_mark", font=(f.symbol, -13), foreground=ACCENT)
        t.tag_configure("typing", font=f.tk(14, False, "italic"), foreground=SUBTLE, lmargin1=2)
        t.tag_configure("user_line", justify="right", spacing1=6, spacing3=6)
        t.tag_configure("info", font=f.tk(13), foreground=SUBTLE, justify="center", spacing1=6, spacing3=6)
        t.tag_configure("info_icon", font=(f.icons, -12))
        t.tag_configure("error", foreground=DANGER)
        t.tag_configure("welcome_title", font=(f.display, -30), foreground=TEXT, justify="center", spacing1=110, spacing3=8)
        t.tag_configure("welcome_sub", font=f.tk(15), foreground=MUTED, justify="center", spacing3=26)
        t.tag_configure("center", justify="center", spacing1=4, spacing3=4)
        # "Used 2 memories · 1 note" under an answer: opens what Nova used.
        t.tag_configure("context_link", font=f.tk(12), foreground=SUBTLE, lmargin1=2, spacing1=2, spacing3=4)
        t.tag_configure("context_link_hover", foreground=ACCENT, underline=True)
        t.tag_configure("url", foreground=ACCENT, underline=True)
        # Search: the opened message is briefly tinted; matching words stay marked.
        t.tag_configure("found", background=ACCENT_SOFT)
        t.tag_configure("found_term", background=FOUND_TERM_BG, foreground="#ffffff")
        t.tag_raise("found_term")
        t.tag_raise("sel")

    # ---- editing helpers ----
    def _edit(self):
        self.text.configure(state="normal")

    def _done(self, follow):
        self.text.configure(state="disabled")
        if follow and not self._bulk:
            self.text.see("end")

    # ---- bulk loading (one scroll at the end instead of one per message) ----
    def begin_bulk(self):
        self._bulk = True

    def end_bulk(self):
        self._bulk = False

    def _at_bottom(self):
        return self.text.yview()[1] >= 0.995

    def _clear_welcome(self):
        if self.welcome_visible:
            self.clear()

    def clear(self):
        self._edit()
        self.text.delete("1.0", "end")
        for label in self.bubbles:
            label.master.destroy()
        self.bubbles = []
        for button in self._actions:
            button.destroy()
        self._actions = []
        for mark in self._message_marks:
            self.text.mark_unset(mark)
        self._message_marks = []
        for tag in self.text.tag_names():
            if tag.startswith(("context_link_", "url_")):
                self.text.tag_delete(tag)        # also drops its click bindings
        self._flashed = []
        self.welcome_visible = False
        self._streaming = False
        self._done(False)

    def scroll_to_end(self):
        self.text.see("end")

    def add_link(self, text, command):
        """A small clickable line under the latest answer (no widget: a text tag)."""
        self._clear_welcome()
        follow = not self._bulk and self._at_bottom()
        self._edit()
        self._links += 1
        tag = f"context_link_{self._links}"
        self.text.insert("end", "ⓘ  " + text + "\n", ("context_link", tag))
        self.text.tag_bind(tag, "<Button-1>", lambda _e: command())
        self.text.tag_bind(tag, "<Enter>", lambda _e, t=tag: (self.text.tag_add("context_link_hover", f"{t}.first",
                                                                               f"{t}.last"),
                                                              self.text.configure(cursor="hand2")))
        self.text.tag_bind(tag, "<Leave>", lambda _e: (self.text.tag_remove("context_link_hover", "1.0", "end"),
                                                      self.text.configure(cursor="")))
        self._done(follow)

    def add_action(self, text, command):
        """A centred button line, e.g. "Show earlier messages"."""
        self._clear_welcome()
        self._edit()
        button = ctk.CTkButton(
            self.text, text=text, font=self.fonts.ui(12), height=28, corner_radius=14, fg_color=SURFACE,
            hover_color=SURFACE_2, border_width=1, border_color=BORDER, text_color=MUTED, bg_color=BG,
            command=command,
        )
        bind_tree(button, "<MouseWheel>", self._wheel)
        self._actions.append(button)
        start = self.text.index("end-1c")
        self.text.window_create("end", window=button, padx=4, pady=6)
        self.text.insert("end", "\n")
        self.text.tag_add("center", start, "end-1c")
        self._done(False)

    # ---- locating messages (used by search) ----
    def mark_message(self, key):
        """Remember where the next added message starts, under a name like 'msg_42'."""
        self.text.mark_set(key, "end-1c")
        self.text.mark_gravity(key, "left")
        self._message_marks.append(key)

    def reveal_message(self, key, terms=(), flash_ms=2600):
        """Scroll a marked message to the top, tint it briefly and mark the search terms.

        Returns False if the message isn't in the current view.
        """
        if key not in self._message_marks:
            return False
        t = self.text
        position = self._message_marks.index(key)
        start = t.index(key)
        end = (t.index(self._message_marks[position + 1])
               if position + 1 < len(self._message_marks) else t.index("end-1c"))
        self._end_flash()
        t.tag_remove("found_term", "1.0", "end")
        t.tag_add("found", start, end)
        # Search the rendered text (Markdown markers already removed).
        count = tk.IntVar(self)
        for term in terms:
            index = start
            while True:
                hit = t.search(term, index, stopindex=end, nocase=True, count=count)
                if not hit or count.get() == 0:
                    break
                hit_end = f"{hit}+{count.get()}c"
                t.tag_add("found_term", hit, hit_end)
                index = hit_end
        # User messages are embedded bubbles, so tint the bubble itself.
        for _kind, name, _index in t.dump(start, end, window=True):
            try:
                holder = self.nametowidget(name)
                holder.configure(fg_color=ACCENT_SOFT, border_width=1, border_color=ACCENT)
                self._flashed.append(holder)
            except (KeyError, tk.TclError, ValueError):
                pass

        def scroll():
            try:
                t.update_idletasks()
                t.yview(start)
            except tk.TclError:
                pass

        scroll()
        self.after(150, scroll)  # again once wrapped bubbles have their final height
        self._flash_job = self.after(flash_ms, self._end_flash)
        return True

    def _end_flash(self):
        if self._flash_job:
            try:
                self.after_cancel(self._flash_job)
            except tk.TclError:
                pass
            self._flash_job = None
        try:
            self.text.tag_remove("found", "1.0", "end")
        except tk.TclError:
            return
        for holder in self._flashed:
            try:
                holder.configure(fg_color=USER_BUBBLE, border_width=0)
            except tk.TclError:
                pass
        self._flashed = []

    # ---- messages ----
    def show_welcome(self, suggestions):
        self.clear()
        self._edit()
        self.text.insert("end", "How can I help today?\n", "welcome_title")
        self.text.insert(
            "end",
            "Nova runs entirely on this PC — your chats, memories and voice never leave it.\n",
            "welcome_sub",
        )
        for suggestion in suggestions:
            chip = ctk.CTkButton(
                self.text, text=suggestion, font=self.fonts.ui(13), height=34,
                corner_radius=17, fg_color=SURFACE, hover_color=SURFACE_2,
                border_width=1, border_color=BORDER, text_color=TEXT, bg_color=BG,
                command=lambda s=suggestion: self.on_suggestion and self.on_suggestion(s),
            )
            bind_tree(chip, "<MouseWheel>", self._wheel)
            self.text.window_create("end", window=chip, padx=5, pady=5)
        self.text.insert("end", "\n", "center")
        self.text.tag_add("center", "3.0", "end")
        self.welcome_visible = True
        self._done(False)

    def add_user(self, content):
        self._clear_welcome()
        follow = True
        self._edit()
        holder = ctk.CTkFrame(self.text, fg_color=USER_BUBBLE, corner_radius=18, bg_color=BG)
        label = ctk.CTkLabel(
            holder, text=content.strip(), font=self.fonts.ui(15), text_color=TEXT,
            justify="left", anchor="w", wraplength=int(self._column_width * 0.72),
        )
        label.pack(padx=16, pady=9)
        for widget in (holder, label):
            bind_tree(widget, "<MouseWheel>", self._wheel)
            bind_tree(widget, "<Button-3>", lambda e, c=content: self._bubble_menu(e, c))
        self.bubbles.append(label)
        self.text.insert("end", "\n", "msg_gap")
        start = self.text.index("end-1c")
        self.text.window_create("end", window=holder)
        self.text.insert("end", "\n")
        self.text.tag_add("user_line", start, "end-1c")
        self._done(follow)

    def _role_line(self):
        self.text.insert("end", "\n", "msg_gap")
        self.text.insert("end", "✦ ", ("role", "role_mark"))
        self.text.insert("end", "Nova\n", "role")

    def add_assistant(self, content):
        self._clear_welcome()
        follow = not self._bulk and self._at_bottom()
        self._edit()
        self._role_line()
        self._render_markdown(content)
        self._done(follow)

    def add_info(self, content, error=False):
        self._clear_welcome()
        follow = not self._bulk and self._at_bottom()
        self._edit()
        tags = ("info", "error") if error else ("info",)
        self.text.insert("end", (Icon.WARNING if error else Icon.INFO) + "  ", tags + ("info_icon",))
        self.text.insert("end", content.strip() + "\n", tags)
        self._done(follow)

    # ---- streaming replies ----
    def begin_assistant(self):
        self._clear_welcome()
        self._edit()
        self._role_line()
        self.text.mark_set("stream_start", "end-1c")
        self.text.mark_gravity("stream_start", "left")
        self.text.insert("end", "Thinking…", "typing")
        self._stream_text = ""
        self._streaming = True
        self._done(True)

    def stream(self, piece):
        if not self._streaming:
            return
        follow = self._at_bottom()
        self._edit()
        if not self._stream_text:
            self.text.delete("stream_start", "end-1c")   # remove "Thinking…"
        self._stream_text += piece
        self.text.insert("end-1c", piece, "body")
        self._done(follow)

    def end_assistant(self):
        """Replace the raw streamed text with its Markdown rendering."""
        if not self._streaming:
            return
        self._streaming = False
        follow = self._at_bottom()
        self._edit()
        self.text.delete("stream_start", "end-1c")
        if self._stream_text.strip():
            self._render_markdown(self._stream_text)
        self._done(follow)

    # ---- Markdown ----
    def _insert_inline(self, line, base):
        pos = 0
        for match in _INLINE.finditer(line):
            if match.start() > pos:
                self.text.insert("end-1c", line[pos:match.start()], base)
            if match.group("url") is not None:
                self._links += 1
                tag = f"url_{self._links}"
                url = match.group("url")
                self.text.insert("end-1c", url, base + ("url", tag))
                self.text.tag_bind(tag, "<Button-1>", lambda _e, u=url: open_web_link(u))
                self.text.tag_bind(tag, "<Enter>", lambda _e: self.text.configure(cursor="hand2"))
                self.text.tag_bind(tag, "<Leave>", lambda _e: self.text.configure(cursor=""))
            elif match.group("bi") is not None:
                self.text.insert("end-1c", match.group("bi"), base + ("bold_italic",))
            elif match.group("b") is not None:
                self.text.insert("end-1c", match.group("b"), base + ("bold",))
            elif match.group("code") is not None:
                self.text.insert("end-1c", match.group("code"), base + ("inline_code",))
            else:
                self.text.insert("end-1c", match.group("i"), base + ("italic",))
            pos = match.end()
        self.text.insert("end-1c", line[pos:] + "\n", base)

    def _insert_code(self, lines, lang):
        while lines and not lines[-1].strip():
            lines.pop()
        self.text.insert("end-1c", "\n", "gap")
        self.text.insert("end-1c", (lang or "code") + "\n", "code_head")
        self.text.insert("end-1c", "\n", "code_pad")
        self.text.insert("end-1c", "\n".join(lines) + "\n", "code")
        self.text.insert("end-1c", "\n", "code_pad")
        self.text.insert("end-1c", "\n", "gap")

    def _render_markdown(self, md):
        lines = str(md).replace("\r\n", "\n").strip("\n").split("\n")
        in_code, lang, code_lines = False, "", []
        last_blank = True
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("```"):
                if in_code:
                    self._insert_code(code_lines, lang)
                    in_code, last_blank = False, True
                else:
                    in_code, lang, code_lines = True, stripped[3:].strip(), []
                continue
            if in_code:
                code_lines.append(raw.rstrip())
                continue
            if not stripped or _HR.match(stripped):
                if not last_blank:
                    self.text.insert("end-1c", "\n", "gap")
                last_blank = True
                continue
            last_blank = False
            if match := _HEADING.match(stripped):
                level = min(len(match.group(1)), 3)
                self._insert_inline(match.group(2).strip("# "), ("body", f"h{level}"))
            elif match := _BULLET.match(raw):
                level = min(len(match.group(1).expandtabs(4)) // 2, 2)
                tag = f"li{level}"
                self.text.insert("end-1c", ("•", "◦", "▪")[level] + "  ", ("body", tag, "bullet_mark"))
                self._insert_inline(match.group(2), ("body", tag))
            elif match := _NUMBERED.match(raw):
                level = min(len(match.group(1).expandtabs(4)) // 2, 2)
                tag = f"li{level}"
                self.text.insert("end-1c", match.group(2) + ".  ", ("body", tag, "bullet_mark"))
                self._insert_inline(match.group(3), ("body", tag))
            elif match := _QUOTE.match(stripped):
                self._insert_inline(match.group(1), ("body", "quote"))
            elif stripped.startswith("|"):
                if not _TABLE_RULE.match(stripped):
                    self.text.insert("end-1c", stripped + "\n", ("body", "table"))
            else:
                self._insert_inline(raw.rstrip(), ("body",))
        if in_code:  # unterminated fence
            self._insert_code(code_lines, lang)

    # ---- copy menus ----
    def _context_menu(self, event):
        self.menu.delete(0, "end")
        has_selection = bool(self.text.tag_ranges("sel"))
        self.menu.add_command(label="Copy", state="normal" if has_selection else "disabled",
                              command=lambda: self._copy(self.text.get("sel.first", "sel.last")))
        self.menu.add_command(label="Select all", command=lambda: self.text.tag_add("sel", "1.0", "end"))
        self.menu.tk_popup(event.x_root, event.y_root)
        self.menu.grab_release()

    def _bubble_menu(self, event, content):
        self.menu.delete(0, "end")
        self.menu.add_command(label="Copy message", command=lambda: self._copy(content))
        self.menu.tk_popup(event.x_root, event.y_root)
        self.menu.grab_release()

    def _copy(self, value):
        self.clipboard_clear()
        self.clipboard_append(value)

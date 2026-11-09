"""
Global conversation search over Nova's existing SQLite tables.

Read-only: searches sessions.title and messages.content with parameterized
LIKE queries; nothing is added to or changed in the database.

Matching rules
- Every word must appear (AND), as part of a word or anywhere in the text,
  so "pyth err" finds "Python error". "Quoted phrases" must appear as written.
- Case-insensitive for English letters (SQLite's built-in LIKE). Highlighting
  in excerpts is case-insensitive for all letters.
- % and _ are matched literally.

Results are newest first and paged with a keyset (message id), so SQLite can
stop scanning as soon as a page is full and "Load more" never rescans earlier
pages. Only the rows of one page are ever loaded into memory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PAGE_SIZE = 50
TITLE_LIMIT = 8
MAX_TERMS = 8
MAX_TERM_LENGTH = 100
EXCERPT_LENGTH = 220
ESCAPE_CHAR = "!"

_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
# Markdown markers dropped from excerpts so they read like the rendered chat.
_MARKDOWN = re.compile(r"\*\*\*|\*\*|__|`+|^[ \t]{0,3}(?:#{1,6}|>)[ \t]*", re.MULTILINE)


@dataclass
class MessageHit:
    message_id: int
    session_id: int
    role: str
    content: str
    created_at: str
    title: str


@dataclass
class TitleHit:
    session_id: int
    title: str
    updated_at: str
    message_count: int


@dataclass
class SearchPage:
    terms: list
    titles: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    has_more: bool = False
    next_before_id: int | None = None


def parse_query(text):
    """Split a query into search terms.

    Words are separated by spaces; "double quotes" keep a phrase together.
    Case-insensitive duplicates are dropped, and very long or very many terms
    are trimmed so a pasted paragraph can't produce a huge query.
    """
    terms, seen = [], set()
    for phrase, word in _TOKEN.findall(str(text or "")):
        term = " ".join((phrase or word).split())[:MAX_TERM_LENGTH]
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def like_pattern(term):
    """A LIKE pattern matching term anywhere, with %, _ and ! escaped."""
    escaped = (
        term.replace(ESCAPE_CHAR, ESCAPE_CHAR * 2)
        .replace("%", ESCAPE_CHAR + "%")
        .replace("_", ESCAPE_CHAR + "_")
    )
    return f"%{escaped}%"


def _all_terms(column, terms):
    """SQL fragment requiring every term in column; values are bound separately."""
    clause = " AND ".join(f"{column} LIKE ? ESCAPE '{ESCAPE_CHAR}'" for _ in terms)
    return clause, [like_pattern(t) for t in terms]


def search_titles(con, terms, limit=TITLE_LIMIT):
    """Conversations whose title contains every term, most recently active first."""
    if not terms:
        return []
    clause, params = _all_terms("s.title", terms)
    rows = con.execute(
        f"""
        SELECT s.id, s.title, s.updated_at,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
        FROM sessions s
        WHERE {clause}
        ORDER BY s.updated_at DESC, s.id DESC
        LIMIT ?
        """,
        [*params, int(limit)],
    ).fetchall()
    return [TitleHit(int(r[0]), str(r[1] or f"Chat {r[0]}"), str(r[2] or ""), int(r[3])) for r in rows]


def search_messages(con, terms, limit=PAGE_SIZE, before_id=None):
    """One page of messages containing every term, newest first.

    Returns (hits, has_more). Pass the last hit's message_id as before_id to
    get the next page.
    """
    if not terms:
        return [], False
    clause, params = _all_terms("m.content", terms)
    if before_id is not None:
        clause += " AND m.id < ?"
        params.append(int(before_id))
    rows = con.execute(
        f"""
        SELECT m.id, m.session_id, m.role, m.content, m.created_at, s.title
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE {clause}
        ORDER BY m.id DESC
        LIMIT ?
        """,
        [*params, int(limit) + 1],  # one extra row tells us whether there's more
    ).fetchall()
    hits = [
        MessageHit(int(r[0]), int(r[1]), str(r[2]), str(r[3] or ""), str(r[4] or ""), str(r[5] or f"Chat {r[1]}"))
        for r in rows[:limit]
    ]
    return hits, len(rows) > limit


def search(con, query, limit=PAGE_SIZE, before_id=None):
    """Search titles (first page only) and messages for a query string."""
    terms = parse_query(query)
    page = SearchPage(terms=terms)
    if not terms:
        return page
    if before_id is None:
        page.titles = search_titles(con, terms)
    page.messages, page.has_more = search_messages(con, terms, limit, before_id)
    if page.messages:
        page.next_before_id = page.messages[-1].message_id
    return page


def session_exists(con, session_id):
    return con.execute("SELECT 1 FROM sessions WHERE id = ?", (int(session_id),)).fetchone() is not None


# -------------------- excerpts and highlighting --------------------
def term_regex(terms):
    """Case-insensitive regex matching any term, longest first."""
    if not terms:
        return None
    alternatives = sorted({re.escape(t) for t in terms}, key=len, reverse=True)
    return re.compile("|".join(alternatives), re.IGNORECASE)


def match_spans(text, terms):
    """[(start, end)] of every term occurrence in text, non-overlapping."""
    pattern = term_regex(terms)
    return [m.span() for m in pattern.finditer(text)] if pattern else []


def make_excerpt(content, terms, length=EXCERPT_LENGTH):
    """Return (excerpt, spans): a one-line snippet around the first match.

    The snippet starts a little before the first matching term, cut at word
    boundaries, with … where text was dropped. Markdown emphasis, code and
    heading markers are removed. spans locate each term occurrence within
    the excerpt for highlighting.
    """
    text = " ".join(_MARKDOWN.sub("", str(content)).split())
    if not text:
        return "", []
    pattern = term_regex(terms)
    first = pattern.search(text) if pattern else None
    if len(text) <= length:
        start, end = 0, len(text)
    else:
        anchor = first.start() if first else 0
        start = max(0, anchor - length // 4)
        end = min(len(text), start + length)
        start = max(0, end - length)
        if start > 0:
            space = text.find(" ", start, anchor if first else start + 30)
            if space != -1:
                start = space + 1
        if end < len(text):
            space = text.rfind(" ", max(start, (first.end() if first else start)), end)
            if space != -1:
                end = space
    excerpt = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    excerpt = prefix + excerpt + suffix
    return excerpt, match_spans(excerpt, terms)

"""
Nova reminders: natural-language parsing, recurrence, scheduling and alert
bookkeeping on the existing nova_reminders table. No GUI code.

Times are local wall-clock times stored as "YYYY-MM-DD HH:MM" (the format
Nova has always used), so "every Monday at 8 PM" stays 8 PM across
daylight-saving changes. Durations ("in 45 minutes", snoozes) are computed
on real elapsed time and converted back to local time, so they are correct
across a clock change too.

Reminders can only alert while Nova is running (window open or in the tray).
Anything that came due while Nova was closed, asleep or Windows was off is
shown as missed the next time Nova starts.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FMT = "%Y-%m-%d %H:%M"
MAX_AHEAD_DAYS = 2 * 366
MISSED_GRACE_MINUTES = 2      # an alert this late was missed (Nova closed, PC asleep)
MIN_INTERVAL = 5              # shortest “every N minutes” repeat (Step 33's scheduled checks)
MAX_TITLE = 200
DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-five": 45, "forty five": 45, "sixty": 60, "ninety": 90,
}
CANCEL_WORDS = {"cancel", "never mind", "nevermind", "no", "nope", "stop", "forget it", "don't", "dont"}

_DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?s?"
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_NUM = r"(?:\d{1,4}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty(?:[- ]five)?|sixty|ninety)"


# ==================== time helpers ====================
def fmt(moment):
    return moment.strftime(FMT)


def parse_stamp(text):
    return datetime.strptime(str(text).strip()[:16].replace("T", " "), FMT)


def clock(moment_or_hm):
    """'9:00 AM' style time."""
    if isinstance(moment_or_hm, str):
        hour, minute = (int(x) for x in moment_or_hm.split(":"))
    else:
        hour, minute = moment_or_hm.hour, moment_or_hm.minute
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def when(moment, now=None):
    """'today at 9:00 AM', 'tomorrow at …', 'Mon 10 Nov at …' (year if not this year)."""
    now = now or datetime.now()
    days = (moment.date() - now.date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif days == -1:
        day = "yesterday"
    else:
        day = f"{moment.strftime('%a')} {moment.day} {moment.strftime('%b')}"
        if moment.year != now.year:
            day += f" {moment.year}"
    return f"{day} at {clock(moment)}"


def shift(now, delta, tz=None):
    """now + an elapsed duration, as local wall-clock time.

    Done in UTC so a daylight-saving change in between is accounted for.
    now is a naive local time; tz defaults to this PC's time zone.
    """
    aware = now.replace(tzinfo=tz) if tz else now.astimezone()
    later = aware.astimezone(timezone.utc) + delta
    local = later.astimezone(tz) if tz else later.astimezone()
    return local.replace(tzinfo=None)


def elapsed_minutes(start, end, tz=None):
    """Real minutes between two local wall times (correct across clock changes)."""
    def utc(moment):
        aware = moment.replace(tzinfo=tz) if tz else moment.astimezone()
        return aware.astimezone(timezone.utc)
    return (utc(end) - utc(start)).total_seconds() / 60


def ceil_minute(moment):
    if moment.second or moment.microsecond:
        moment = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return moment


# ==================== recurrence ====================
def validate_rule(rule):
    """Return the rule if it's one Nova supports, else raise ValueError.

    daily | weekly:MO,WE,... | monthly:1..31 | monthly:last | every:N (minutes, 5..10080; Step 33's checks)
    """
    kind, _, arg = str(rule).partition(":")
    if kind == "daily" and not arg:
        return rule
    if kind == "every" and arg.isdigit() and MIN_INTERVAL <= int(arg) <= 7 * 24 * 60:
        return rule
    if kind == "weekly":
        codes = arg.split(",")
        if codes and all(c in DAY_CODES for c in codes) and len(set(codes)) == len(codes):
            return "weekly:" + ",".join(sorted(codes, key=DAY_CODES.index))
    if kind == "monthly" and (arg == "last" or (arg.isdigit() and 1 <= int(arg) <= 31)):
        return rule
    raise ValueError(f"Unsupported repeat rule: {rule}")


def next_occurrence(rule, anchor, after):
    """The first occurrence strictly after `after` (naive local wall time).

    Monthly days beyond a month's length fall on its last day (the 31st is
    30 April, 28/29 February). Wall-clock arithmetic keeps the time of day
    fixed across daylight-saving changes.
    """
    hour, minute = (int(x) for x in anchor.split(":"))
    kind, _, arg = validate_rule(rule).partition(":")
    if kind == "every":            # a steady interval, counted from a fixed origin at the anchor time
        origin, step = datetime(2000, 1, 1, hour, minute), int(arg)
        passed = int((after - origin).total_seconds() // 60)
        return origin + timedelta(minutes=(passed // step + 1) * step)
    if kind == "daily":
        candidate = datetime.combine(after.date(), datetime.min.time()).replace(hour=hour, minute=minute)
        return candidate if candidate > after else candidate + timedelta(days=1)
    if kind == "weekly":
        days = {DAY_CODES.index(code) for code in arg.split(",")}
        for offset in range(8):
            day = after.date() + timedelta(days=offset)
            candidate = datetime(day.year, day.month, day.day, hour, minute)
            if day.weekday() in days and candidate > after:
                return candidate
    if kind == "monthly":
        year, month = after.year, after.month
        for _ in range(14):
            last = calendar.monthrange(year, month)[1]
            day = last if arg == "last" else min(int(arg), last)
            candidate = datetime(year, month, day, hour, minute)
            if candidate > after:
                return candidate
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise ValueError(f"No next occurrence for {rule}")


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe_rule(rule, anchor):
    kind, _, arg = validate_rule(rule).partition(":")
    at = clock(anchor)
    if kind == "every":
        minutes = int(arg)
        if minutes % 1440 == 0:
            return f"every {minutes // 1440} days at {at}" if minutes > 1440 else f"every day at {at}"
        if minutes % 60 == 0:
            return "every hour" if minutes == 60 else f"every {minutes // 60} hours"
        return f"every {minutes} minutes"
    if kind == "daily":
        return f"every day at {at}"
    if kind == "weekly":
        codes = arg.split(",")
        if codes == DAY_CODES[:5]:
            return f"every weekday at {at}"
        if codes == ["SA", "SU"]:
            return f"every weekend day at {at}"
        names = [DAY_NAMES[DAY_CODES.index(c)] for c in codes]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"every {joined} at {at}"
    if arg == "last":
        return f"on the last day of every month at {at}"
    note = " (the last day in shorter months)" if int(arg) > 28 else ""
    return f"on the {_ordinal(int(arg))} of every month at {at}{note}"


# ==================== natural-language parsing ====================
@dataclass
class Schedule:
    due: datetime                 # first alert, naive local wall time
    rule: str | None = None       # None for one-off reminders
    anchor: str | None = None     # "HH:MM" for recurring reminders
    notes: list = field(default_factory=list)


@dataclass
class Clarify:
    """A question Nova asks instead of guessing, with how each answer rewrites the request."""
    question: str
    text: str                     # the request, normalized, to rewrite with the answer
    kind: str                     # time | title | choice
    options: list = field(default_factory=list)   # [(keywords, rewritten text)]


@dataclass
class Parsed:
    kind: str                     # none | ok | clarify | error | cancelled
    title: str = ""
    schedule: Schedule | None = None
    message: str = ""
    clarify: Clarify | None = None


class _Found:
    def __init__(self, text):
        self.original = text
        self.masked = text

    def take(self, pattern, flags=re.IGNORECASE):
        """Find a pattern in the not-yet-used text and blank it out."""
        match = re.search(pattern, self.masked, flags)
        if match:
            start, end = match.span()
            self.masked = self.masked[:start] + " " * (end - start) + self.masked[end:]
        return match

    def rest(self):
        return self.masked


_PREFIX = re.compile(
    r"^\s*(?:hey\s+)?(?:nova[,:]?\s+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?"
    r"(?:remind\s+me|set\s+(?:a\s+|up\s+a\s+)?reminder(?:\s+for\s+me)?|create\s+a\s+reminder|add\s+a\s+reminder)\b",
    re.IGNORECASE,
)


def is_reminder_request(text):
    return bool(_PREFIX.match(str(text or "")))


def _number(word):
    word = word.lower()
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


def _hint(found):
    if found.take(r"\b(?:tonight|this\s+evening|in\s+the\s+evening|at\s+night|in\s+the\s+afternoon|this\s+afternoon)\b"):
        return "pm"
    if found.take(r"\b(?:in\s+the\s+morning|this\s+morning)\b"):
        return "am"
    # "tomorrow morning", "Friday evening" (after the day word has been read)
    if found.take(r"\bmorning\b"):
        return "am"
    if found.take(r"\b(?:evening|afternoon)\b"):
        return "pm"
    return None


def _time(found):
    """(hour, minute) | 'ambiguous:<hour>:<minute>' | None, consuming the phrase."""
    m = found.take(r"\b(?:at\s+|@\s*|by\s+)?(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)(?=\W|$)")
    if m:
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise ValueError("That isn't a valid time.")
        pm = m.group("ap").lower().startswith("p")
        return (hour % 12 + (12 if pm else 0), minute)
    m = found.take(r"\b(?:at\s+)?(?P<word>noon|midday|midnight)\b")
    if m:
        return (12, 0) if m.group("word").lower() != "midnight" else (0, 0)
    m = found.take(r"(?:\bat\s+|@\s*)(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\b(?!\s*(?:minutes?|mins?|hours?|days?|%))")
    if not m:
        m = found.take(r"(?<![\d/-])\b(?P<h>\d{2}):(?P<m>\d{2})\b")
    if m:
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        if hour > 23 or minute > 59:
            raise ValueError("That isn't a valid time.")
        two_digit = len(m.group("h")) == 2 and m.group("m") is not None
        if hour == 0 or hour >= 13 or two_digit:
            return (hour, minute)
        return f"ambiguous:{hour}:{minute}"
    return None


def _apply_hint(value, hint):
    if isinstance(value, str) and value.startswith("ambiguous:") and hint:
        hour, minute = (int(x) for x in value.split(":")[1:])
        return (hour % 12 + (12 if hint == "pm" else 0), minute)
    return value


def _weekday_list(text):
    days = []
    for word in re.findall(r"[a-z]+", text.lower()):
        word = word[:-1] if word.endswith("s") and word[:-1] in WEEKDAYS else word
        if word in WEEKDAYS and DAY_CODES[WEEKDAYS[word]] not in days:
            days.append(DAY_CODES[WEEKDAYS[word]])
    return days


def _recurrence(found):
    """A repeat rule found in the text, or None. Raises ValueError for unsupported repeats."""
    if found.take(r"\b(?:every\s+other|every\s+\d+|every\s+(?:two|three|four|five|six)|fortnightly|"
                  r"every\s+fortnight|biweekly|every\s+year|yearly|annually|every\s+hour|hourly|"
                  r"every\s+\d*\s*minutes?)\b"):
        raise ValueError("I can repeat reminders every day, every week on chosen days, or every month on a "
                         "date, but not on that schedule. Set it up as separate reminders instead.")
    m = found.take(r"\b(?:on\s+)?the\s+last\s+day\s+of\s+(?:every|each|the)\s+month\b"
                   r"|\b(?:every|each)\s+month\s+on\s+the\s+last\s+day\b|\bmonthly\s+on\s+the\s+last\s+day\b")
    if m:
        return "monthly:last"
    m = (found.take(r"\b(?:on\s+)?the\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:\s+day)?\s+of\s+(?:every|each)\s+month\b")
         or found.take(r"\b(?:every|each)\s+month\s+on\s+the\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b")
         or found.take(r"\bmonthly\s+on\s+the\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b"))
    if m:
        day = int(m.group("d"))
        if not 1 <= day <= 31:
            raise ValueError("Months only have days 1 to 31.")
        return f"monthly:{day}"
    if found.take(r"\b(?:every|each)\s+month\b|\bmonthly\b"):
        return "ask:month"
    if found.take(r"\b(?:every|each)\s+weekday\b|\bon\s+weekdays\b|\bweekdays\b"):
        return "weekly:" + ",".join(DAY_CODES[:5])
    if found.take(r"\b(?:every|each)\s+weekend(?:\s+day)?\b|\bon\s+weekends\b|\bweekends\b"):
        return "weekly:SA,SU"
    m = (found.take(rf"\b(?:every|each)\s+(?:week\s+on\s+)?(?P<days>{_DAY}(?:\s*(?:,|and|&|or)\s*{_DAY})*)\b")
         or found.take(rf"\b(?:weekly|every\s+week)\s+on\s+(?P<days>{_DAY}(?:\s*(?:,|and|&)\s*{_DAY})*)\b")
         or found.take(rf"\bon\s+(?P<days>(?:mon|tues|wednes|thurs|fri|satur|sun)days(?:\s*(?:,|and|&)\s*{_DAY})*)\b"))
    if m:
        return "weekly:" + ",".join(sorted(_weekday_list(m.group("days")), key=DAY_CODES.index))
    if found.take(r"\b(?:every|each)\s+week\b|\bweekly\b"):
        return "ask:week"
    if found.take(r"\b(?:every|each)\s+(?:day|morning|evening|night|afternoon)\b|\bdaily\b"):
        return "daily"
    return None


def _relative(found):
    """(timedelta or day count, unit kind) from 'in 45 minutes', 'in an hour and a half', 'in 2 days'."""
    m = found.take(rf"\bin\s+half\s+an?\s+hour\b")
    if m:
        return timedelta(minutes=30), "time"
    m = found.take(rf"\bin\s+(?P<n>{_NUM})\s*(?P<u>minutes?|mins?|m|hours?|hrs?|h|days?|weeks?)\b"
                   rf"(?:\s+(?:and\s+)?(?:(?P<half>a\s+half)|(?P<n2>\d{{1,2}})\s*(?P<u2>minutes?|mins?|m)))?")
    if not m:
        return None, None
    n, unit = _number(m.group("n")), m.group("u").lower()
    if unit.startswith("w"):
        return n * 7, "days"
    if unit.startswith("d"):
        return n, "days"
    minutes = n * 60 if unit.startswith("h") else n
    if m.group("half"):
        minutes += 30 if unit.startswith("h") else 0
    if m.group("n2"):
        minutes += int(m.group("n2"))
    return timedelta(minutes=minutes), "time"


def _date(found, now):
    """A specific date or ('weekday', n, mode) or ('ask_order', a, b, span) or None."""
    m = found.take(r"\b(?:on\s+)?(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})\b")
    if m:
        try:
            return date(int(m.group("y")), int(m.group("mo")), int(m.group("d")))
        except ValueError:
            raise ValueError(f"{m.group(0).strip()} isn't a real date.") from None
    m = found.take(r"\b(?:the\s+)?day\s+after\s+tomorrow\b")
    if m:
        return now.date() + timedelta(days=2)
    m = found.take(r"\b(?:today|tonight)\b")
    if m:
        return now.date()
    m = found.take(r"\btomorrow\b")
    if m:
        return now.date() + timedelta(days=1)
    m = (found.take(rf"\b(?:on\s+)?(?:the\s+)?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mo>{_MONTH})\b(?:,?\s+(?P<y>\d{{4}}))?")
         or found.take(rf"\b(?:on\s+)?(?P<mo>{_MONTH})\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y>\d{{4}}))?"))
    if m:
        name = m.group("mo").lower()
        month = MONTHS.get(name) or MONTHS[name[:3]]
        day = int(m.group("d"))
        year = int(m.group("y")) if m.group("y") else None
        return ("named", day, month, year)
    m = found.take(r"\b(?:on\s+)?(?P<a>\d{1,2})/(?P<b>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
    if m:
        a, b = int(m.group("a")), int(m.group("b"))
        year = m.group("y")
        year = (2000 + int(year) if year and len(year) == 2 else int(year)) if year else None
        if a > 12 and b <= 12:
            return ("named", a, b, year)
        if b > 12 and a <= 12:
            return ("named", b, a, year)
        if a == b:
            return ("named", a, b, year)
        return ("ask_order", a, b, year, m.group(0).strip())
    m = found.take(rf"\b(?P<mode>on|next|this|coming)\s+(?P<day>{_DAY})\b")
    if m:
        weekday = DAY_CODES.index(_weekday_list(m.group("day"))[0])
        return ("weekday", weekday, m.group("mode").lower(), m.group(0).strip())
    m = found.take(r"\b(?:on\s+)?the\s+(?P<d>\d{1,2})(?:st|nd|rd|th)\b")
    if m:
        return ("day_of_month", int(m.group("d")))
    return None


def _resolve_named(day, month, year, now):
    """A named date; without a year, the next time it comes round."""
    try:
        if year:
            return date(year, month, day), None
        candidate = date(now.year, month, day)
        if candidate < now.date():
            return date(now.year + 1, month, day), "next year"
        return candidate, None
    except ValueError:
        raise ValueError(f"{calendar.month_name[month]} doesn't have a {_ordinal(day)}.") from None


def _title(found):
    text = re.sub(r"\s+", " ", found.rest()).strip(" ,.;:!-")
    text = re.sub(r"^(?:(?:to|about|that|of|for|me)\s+)+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:to|at|on|for|and|by)$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,.;:!-")[:MAX_TITLE]


def parse_reminder(text, now=None, tz=None):
    """Understand a reminder request. Never guesses: unclear parts become a question.

    now: naive local time (defaults to the PC clock); tz: for tests only.
    """
    now = (now or datetime.now()).replace(microsecond=0)
    raw = str(text or "").strip()
    prefix = _PREFIX.match(raw)
    if not prefix:
        return Parsed("none")
    body = raw[prefix.end():].strip().rstrip(".!")
    normalized = "remind me " + body
    found = _Found(" " + body + " ")
    try:
        rule = _recurrence(found)
        hint = _hint(found)
        if rule == "daily" and not hint:  # "every morning/evening at 7"
            daypart = re.search(r"\b(?:every|each)\s+(morning|evening|night|afternoon)\b", body, re.IGNORECASE)
            hint = ("am" if daypart.group(1).lower() == "morning" else "pm") if daypart else None
        relative, relative_kind = _relative(found)
        when_date = _date(found, now)
        hint = hint or _hint(found)
        clock_time = _apply_hint(_time(found), hint)
    except ValueError as exc:
        return Parsed("error", message=str(exc))
    if rule == "ask:week" and isinstance(when_date, tuple) and when_date[0] == "weekday":
        rule, when_date = f"weekly:{DAY_CODES[when_date[1]]}", None   # "every week … on Monday"
    title = _title(found)

    if not title:
        return Parsed("clarify", clarify=Clarify("What should I remind you about?", normalized, "title"),
                      message="What should I remind you about?")

    def ask_time(where):
        question = f"What time {where} should I remind you about “{title}”? (for example 9 AM or 18:30)"
        return Parsed("clarify", title=title, message=question, clarify=Clarify(question, normalized, "time"))

    if isinstance(clock_time, str):  # "at 9" with no am/pm
        hour, minute = (int(x) for x in clock_time.split(":")[1:])
        if hour == 12 and minute == 0:
            question = "Do you mean 12 noon or 12 midnight?"
            options = [
                (("noon", "midday", "pm", "p.m", "day", "lunch", "afternoon"), _replace_time(normalized, 12, 0, 12)),
                (("midnight", "am", "a.m", "night"),
                 re.sub(r"(?:\bat\s+|@\s*)12(?:[:.]00)?\b", "at midnight", normalized, count=1, flags=re.IGNORECASE)),
            ]
        else:
            question = f"Do you mean {hour}:{minute:02d} in the morning or the evening? (AM or PM)"
            options = [
                (("am", "a.m", "morning"), _replace_time(normalized, hour, minute, 0)),
                (("pm", "p.m", "evening", "afternoon", "night", "tonight"), _replace_time(normalized, hour, minute, 12)),
            ]
        return Parsed("clarify", title=title, message=question, clarify=Clarify(question, normalized, "choice", options))

    # ---- recurring ----
    if rule:
        if relative or (when_date and not (isinstance(when_date, tuple) and when_date[0] == "weekday")):
            return Parsed("error", message="A repeating reminder can't also have a one-off date or delay. "
                                           "Say just the repeat, e.g. “every Monday at 8 PM”.")
        if rule in ("ask:week", "ask:month"):
            weekly = rule == "ask:week"
            template = re.sub(r"\b(?:every|each)\s+week\b|\bweekly\b" if weekly else
                              r"\b(?:every|each)\s+month\b|\bmonthly\b", "{RULE}", normalized, count=1,
                              flags=re.IGNORECASE)
            question = ("Which day of the week should it repeat on? (for example “Monday” or “weekdays”)"
                        if weekly else "On which day of the month? (for example “the 15th” or “the last day”)")
            if clock_time is None:
                question = question[:-1] + ", and at what time)"
            return Parsed("clarify", title=title, message=question,
                          clarify=Clarify(question, template, "week" if weekly else "month"))
        if clock_time is None:
            return ask_time(describe_rule(rule, "00:00").rsplit(" at ", 1)[0])
        anchor = f"{clock_time[0]:02d}:{clock_time[1]:02d}"
        first = next_occurrence(rule, anchor, now)
        return Parsed("ok", title=title, schedule=Schedule(first, rule, anchor))

    # ---- relative ----
    notes = []
    if relative is not None:
        if relative_kind == "time":
            if clock_time is not None or when_date is not None:
                return Parsed("error", message="Give either a delay (“in 45 minutes”) or a time (“at 9 AM”), not both.")
            if relative < timedelta(minutes=1):
                return Parsed("error", message="The delay must be at least one minute.")
            due = ceil_minute(shift(now, relative, tz))
            return _checked(title, Schedule(due), now)
        if when_date is not None:
            return Parsed("error", message="Give either “in N days” or a date, not both.")
        when_date = now.date() + timedelta(days=relative)

    # ---- dates ----
    if isinstance(when_date, tuple):
        kind = when_date[0]
        try:
            if kind == "named":
                when_date, note = _resolve_named(when_date[1], when_date[2], when_date[3], now)
                if note:
                    notes.append(note)
            elif kind == "ask_order":
                a, b, year, phrase = when_date[1:]
                first = date(year or now.year, b, a)
                second = date(year or now.year, a, b)
                digits = re.sub(r"^on\s+", "", phrase, flags=re.IGNORECASE)
                question = (f"Is {digits} {first.day} {first.strftime('%B')} or {second.strftime('%B')} "
                            f"{second.day}? Please write the month name.")
                options = [((first.strftime("%B").lower(), first.strftime("%b").lower()),
                            normalized.replace(phrase, f"on {first.day} {first.strftime('%B')}"
                                                       + (f" {year}" if year else ""), 1)),
                           ((second.strftime("%B").lower(), second.strftime("%b").lower()),
                            normalized.replace(phrase, f"on {second.day} {second.strftime('%B')}"
                                                       + (f" {year}" if year else ""), 1))]
                return Parsed("clarify", title=title, message=question,
                              clarify=Clarify(question, normalized, "choice", options))
            elif kind == "weekday":
                weekday, mode, phrase = when_date[1:]
                ahead = (weekday - now.weekday()) % 7
                if mode == "next" and ahead == 0:
                    ahead = 7
                if ahead == 0:
                    later_today = clock_time is not None and datetime.combine(
                        now.date(), datetime.min.time()).replace(hour=clock_time[0], minute=clock_time[1]) > now
                    if not later_today:
                        ahead = 7
                    else:
                        name = DAY_NAMES[weekday]
                        question = f"Today is {name}. Do you mean today or next {name}?"
                        options = [(("today",), normalized.replace(phrase, "today", 1)),
                                   (("next", "week"), normalized.replace(phrase, f"next {name}", 1))]
                        return Parsed("clarify", title=title, message=question,
                                      clarify=Clarify(question, normalized, "choice", options))
                when_date = now.date() + timedelta(days=ahead)
            elif kind == "day_of_month":
                day = when_date[1]
                year, month = now.year, now.month
                for _ in range(13):
                    if day <= calendar.monthrange(year, month)[1]:
                        candidate = date(year, month, day)
                        if candidate > now.date() or (candidate == now.date() and clock_time and datetime.combine(
                                candidate, datetime.min.time()).replace(hour=clock_time[0], minute=clock_time[1]) > now):
                            when_date = candidate
                            break
                    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
                else:
                    return Parsed("error", message=f"No month soon has a {_ordinal(day)}.")
        except ValueError as exc:
            return Parsed("error", message=str(exc))

    if when_date is None and clock_time is None:
        question = (f"When should I remind you about “{title}”? (for example “tomorrow at 9 AM”, "
                    "“in 45 minutes” or “every Monday at 8 PM”)")
        return Parsed("clarify", title=title, message=question, clarify=Clarify(question, normalized, "when"))
    if clock_time is None:
        return ask_time(f"on {when_date.strftime('%A')} {when_date.day} {when_date.strftime('%B')}"
                        if when_date != now.date() else "today")
    midnight = clock_time == (0, 0) and re.search(r"\bmidnight\b", normalized, re.IGNORECASE)
    if when_date is None and midnight:
        when_date = now.date()  # "at midnight" = tonight, at the end of today
    if when_date is None:
        when_date = now.date()
        due = datetime(when_date.year, when_date.month, when_date.day, *clock_time)
        if due <= now:
            question = f"{clock(due)} has already passed today. Do you mean tomorrow at {clock(due)}?"
            options = [(("yes", "yeah", "yep", "sure", "ok", "okay", "tomorrow", "please"), normalized + " tomorrow")]
            return Parsed("clarify", title=title, message=question,
                          clarify=Clarify(question, normalized, "choice", options))
        return _checked(title, Schedule(due, notes=notes), now)
    due = datetime(when_date.year, when_date.month, when_date.day, *clock_time)
    if midnight:
        due += timedelta(days=1)  # "tomorrow at midnight" = the end of tomorrow
        notes.append("midnight at the end of that day")
    return _checked(title, Schedule(due, notes=notes), now)


def _replace_time(text, hour, minute, add):
    """Rewrite an ambiguous 'at 9' as an unambiguous 24-hour time."""
    resolved = f"at {(hour % 12) + add:02d}:{minute:02d}"
    pattern = rf"(?:\bat\s+|@\s*){hour}(?:[:.]{minute:02d})?\b" if minute else rf"(?:\bat\s+|@\s*){hour}(?:[:.]00)?\b"
    rewritten, count = re.subn(pattern, resolved, text, count=1, flags=re.IGNORECASE)
    return rewritten if count else text + " " + resolved


def _checked(title, schedule, now):
    if schedule.due <= now:
        moment = when(schedule.due, now)
        return Parsed("error", message=f"{moment[0].upper()}{moment[1:]} is in the past. Please give a future time.")
    if schedule.due > now + timedelta(days=MAX_AHEAD_DAYS):
        return Parsed("error", message="Reminders can be at most two years ahead.")
    return Parsed("ok", title=title, schedule=schedule)


def answer(clarify, reply, now=None, tz=None):
    """Apply the user's answer to a question. Returns Parsed, or None if the reply
    doesn't look like an answer (then treat it as a new message)."""
    text = " ".join(str(reply or "").split())
    low = text.lower().rstrip(".!")
    if low in CANCEL_WORDS:
        return Parsed("cancelled", message="Okay, I won't set that reminder.")
    if clarify.kind == "choice":
        for keywords, rewritten in clarify.options:
            if any(re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low) for k in keywords):
                return parse_reminder(rewritten, now, tz)
        # An explicit time like "9pm" also answers an AM/PM question.
        if "AM or PM" in clarify.question and re.search(r"\d\s*[ap]\.?\s?m", low):
            return parse_reminder(re.sub(r"(?:\bat\s+|@\s*)\d{1,2}(?:[:.]\d{2})?\b", "", clarify.text, count=1)
                                  + " at " + text, now, tz)
        return None
    if clarify.kind in ("week", "month"):
        return _answer_repeat(clarify, text, now, tz)
    if clarify.kind == "time":
        probe = _Found(" " + text + " ")
        try:
            value = _time(probe) or _time(_Found(" at " + text + " "))
        except ValueError:
            return None
        if value is None:
            return None
        if isinstance(value, str):
            hour, minute = (int(x) for x in value.split(":")[1:])
            return parse_reminder(f"{clarify.text} at {hour}:{minute:02d}", now, tz)
        return parse_reminder(f"{clarify.text} at {value[0]:02d}:{value[1]:02d}", now, tz)
    if clarify.kind == "title":
        if len(text) < 2 or is_reminder_request(text):
            return None
        return parse_reminder(f"{clarify.text} to {text}", now, tz)
    if clarify.kind == "when":
        if is_reminder_request(text):
            return None
        result = parse_reminder(f"{clarify.text} {text}", now, tz)
        return result if result.kind != "none" else None
    return None


def _answer_repeat(clarify, text, now, tz):
    """Fill in 'every week' / 'every month' with the day(s) the user named."""
    if is_reminder_request(text):
        return None
    low = text.lower()
    try:
        value = _time(_Found(" " + text + " "))
    except ValueError:
        value = None
    at = ""
    if isinstance(value, tuple):
        at = f" at {value[0]:02d}:{value[1]:02d}"
    elif isinstance(value, str):
        at = " at {}:{:02d}".format(*(int(x) for x in value.split(":")[1:]))
    if clarify.kind == "week":
        if re.search(r"\bweekdays?\b", low):
            phrase = "every weekday"
        else:
            codes = _weekday_list(low)
            if not codes:
                return None
            phrase = "every " + " and ".join(DAY_NAMES[DAY_CODES.index(c)] for c in codes)
    else:
        if re.search(r"\blast\b", low):
            phrase = "on the last day of every month"
        else:
            m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?::|am|pm|a\.m|p\.m))", low)
            if not m:
                return None
            phrase = f"on the {_ordinal(int(m.group(1)))} of every month"
    return parse_reminder(clarify.text.replace("{RULE}", phrase) + at, now, tz)


def schedule_from_fields(date_text, time_text, repeat="once", weekdays=(), month_day="", now=None):
    """Build a Schedule from the exact-time editor. Raises ValueError with a readable message.

    repeat: once | daily | weekly | monthly. For repeats, the date is the
    first day it may start; the first alert is the next occurrence from then.
    """
    now = now or datetime.now()
    try:
        hour, minute = (int(x) for x in str(time_text).strip().split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ValueError("Use a 24-hour time like 09:00 or 18:30.") from None
    try:
        start = datetime.strptime(str(date_text).strip(), "%Y-%m-%d")
    except ValueError:
        raise ValueError("Use a date like 2026-11-20.") from None
    anchor = f"{hour:02d}:{minute:02d}"
    if repeat == "once":
        due = start.replace(hour=hour, minute=minute)
        if due <= now:
            raise ValueError("That time is in the past. Choose a future date and time.")
        if due > now + timedelta(days=MAX_AHEAD_DAYS):
            raise ValueError("Reminders can be at most two years ahead.")
        return Schedule(due)
    if repeat == "daily":
        rule = "daily"
    elif repeat == "weekly":
        codes = [c for c in DAY_CODES if c in set(weekdays)]
        if not codes:
            raise ValueError("Choose at least one day of the week.")
        rule = "weekly:" + ",".join(codes)
    elif repeat == "monthly":
        text = str(month_day).strip().lower()
        if text == "last":
            rule = "monthly:last"
        elif text.isdigit() and 1 <= int(text) <= 31:
            rule = f"monthly:{int(text)}"
        else:
            raise ValueError("For monthly reminders, enter a day from 1 to 31, or “last”.")
    else:
        raise ValueError("Unknown repeat option.")
    after = max(now, start - timedelta(minutes=1))
    return Schedule(next_occurrence(rule, anchor, after), rule, anchor)


# ==================== storage ====================
REMINDER_COLUMNS = {
    "recurrence": "TEXT",          # NULL = one-off; daily | weekly:MO,.. | monthly:N | monthly:last
    "anchor_time": "TEXT",         # "HH:MM" of a recurring reminder
    "occurrence_at": "TEXT",       # the scheduled occurrence the next alert belongs to
    "completed_at": "TEXT",
    "updated_at": "TEXT",
    "parent_id": "INTEGER",        # a snoozed occurrence of a recurring reminder
}

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS nova_reminder_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    occurrence_at TEXT NOT NULL,
    alert_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (reminder_id, occurrence_at, alert_at, kind)
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON nova_reminders(completed, notified_at, due_at);
"""


def _stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def migrate(con, db_path=None, backup=None):
    """Add the scheduling columns and the alert log. Additive and repeatable.

    Existing reminders keep their title, time and state; occurrence_at is
    filled from due_at. A backup copy of the database is made first if any
    reminders exist (backup(con, db_path) is supplied by the caller).
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(nova_reminders)")}
    if not columns:
        raise RuntimeError("nova_reminders doesn't exist yet.")
    missing = [name for name in REMINDER_COLUMNS if name not in columns]
    if missing and backup and db_path and Path(db_path).exists():
        if con.execute("SELECT 1 FROM nova_reminders LIMIT 1").fetchone():
            backup(con, db_path)
    with con:
        for name in missing:  # fixed names and types, never user input
            con.execute(f"ALTER TABLE nova_reminders ADD COLUMN {name} {REMINDER_COLUMNS[name]}")
        con.execute("UPDATE nova_reminders SET occurrence_at = due_at WHERE occurrence_at IS NULL")
    con.executescript(EVENTS_SCHEMA)
    con.commit()
    return missing


SELECT = ("id, title, due_at, completed, created_at, notified_at, recurrence, anchor_time, occurrence_at, "
          "completed_at, updated_at, parent_id")


@dataclass
class Reminder:
    id: int
    title: str
    due: datetime
    completed: bool
    created_at: str
    notified_at: str | None
    rule: str | None
    anchor: str | None
    occurrence: datetime
    completed_at: str | None
    updated_at: str | None
    parent_id: int | None

    @property
    def recurring(self):
        return bool(self.rule)

    def describe(self, now=None):
        if self.rule:
            return f"{describe_rule(self.rule, self.anchor)} · next {when(self.due, now)}"
        return when(self.due, now)


def _reminder(row):
    due = parse_stamp(row[2])
    return Reminder(int(row[0]), str(row[1]), due, bool(row[3]), str(row[4] or ""), row[5], row[6], row[7],
                    parse_stamp(row[8]) if row[8] else due, row[9], row[10], row[11])


def get(con, reminder_id):
    row = con.execute(f"SELECT {SELECT} FROM nova_reminders WHERE id = ?", (int(reminder_id),)).fetchone()
    return _reminder(row) if row else None


def _validate_title(title):
    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("A reminder needs a title.")
    if len(title) > MAX_TITLE:
        raise ValueError(f"Keep the reminder under {MAX_TITLE} characters.")
    return title


def find_duplicate(con, title, schedule, exclude_id=None):
    """An active reminder with the same title and schedule, if any."""
    for row in con.execute(
            f"SELECT {SELECT} FROM nova_reminders WHERE completed = 0 AND lower(title) = lower(?) "
            "AND IFNULL(recurrence, '') = IFNULL(?, '') AND id != ?",
            (_validate_title(title), schedule.rule, exclude_id or -1)):
        existing = _reminder(row)
        if schedule.rule:
            if existing.anchor == schedule.anchor:
                return existing
        elif existing.due == schedule.due:
            return existing
    return None


@dataclass
class Saved:
    reminder: Reminder
    created: bool                 # False: an identical reminder already existed; nothing was changed


def create(con, title, schedule, now=None, parent_id=None, occurrence=None, allow_duplicate=False):
    """Add a reminder. Never overwrites: an identical active one is returned unchanged,
    unless the user explicitly asked for another copy (allow_duplicate)."""
    title = _validate_title(title)
    if schedule.rule:
        validate_rule(schedule.rule)
    existing = find_duplicate(con, title, schedule) if parent_id is None and not allow_duplicate else None
    if existing:
        return Saved(existing, False)
    with con:
        cursor = con.execute(
            "INSERT INTO nova_reminders (title, due_at, completed, created_at, recurrence, anchor_time, "
            "occurrence_at, updated_at, parent_id) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (title, fmt(schedule.due), _stamp(), schedule.rule, schedule.anchor,
             fmt(occurrence or schedule.due), _stamp(), parent_id),
        )
    return Saved(get(con, cursor.lastrowid), True)


def update(con, reminder_id, title, schedule, now=None):
    """Edit one reminder in place (title, time, repeat). Other reminders are never touched."""
    now = now or datetime.now()
    current = get(con, reminder_id)
    if current is None:
        raise ValueError("That reminder no longer exists.")
    title = _validate_title(title)
    if schedule.rule:
        validate_rule(schedule.rule)
    elif schedule.due <= now:
        raise ValueError("Choose a time in the future.")
    with con:
        con.execute(
            "UPDATE nova_reminders SET title = ?, due_at = ?, occurrence_at = ?, recurrence = ?, anchor_time = ?, "
            "notified_at = NULL, completed = 0, completed_at = NULL, updated_at = ? WHERE id = ?",
            (title, fmt(schedule.due), fmt(schedule.due), schedule.rule, schedule.anchor, _stamp(), reminder_id),
        )
        _log(con, reminder_id, schedule.due, schedule.due, "edited")
    return get(con, reminder_id)


def snooze(con, reminder_id, minutes=None, until=None, now=None, tz=None, occurrence=None):
    """Postpone an alert. A one-off reminder moves; for a repeating one, a separate
    one-off copy is created for that occurrence so the series keeps its schedule."""
    now = now or datetime.now()
    reminder = get(con, reminder_id)
    if reminder is None:
        raise ValueError("That reminder no longer exists.")
    due = until or ceil_minute(shift(now, timedelta(minutes=minutes), tz))
    if due <= now:
        raise ValueError("Snooze to a time in the future.")
    if reminder.recurring:
        return create(con, reminder.title, Schedule(due), now, parent_id=reminder.id,
                      occurrence=occurrence or reminder.occurrence).reminder
    with con:
        con.execute("UPDATE nova_reminders SET due_at = ?, notified_at = NULL, completed = 0, updated_at = ? "
                    "WHERE id = ?", (fmt(due), _stamp(), reminder_id))
        _log(con, reminder_id, reminder.occurrence, due, "snoozed")
    return get(con, reminder_id)


def skip_next(con, reminder_id, now=None):
    """Skip the next occurrence of a repeating reminder."""
    reminder = get(con, reminder_id)
    if reminder is None or not reminder.recurring:
        raise ValueError("Only repeating reminders can skip an occurrence.")
    following = next_occurrence(reminder.rule, reminder.anchor, reminder.occurrence)
    with con:
        con.execute("UPDATE nova_reminders SET due_at = ?, occurrence_at = ?, notified_at = NULL, updated_at = ? "
                    "WHERE id = ?", (fmt(following), fmt(following), _stamp(), reminder_id))
        _log(con, reminder_id, reminder.occurrence, reminder.due, "skipped")
    return get(con, reminder_id)


def complete(con, reminder_id, occurrence=None):
    """Mark done. For a repeating reminder, with an occurrence: just that occurrence;
    without: end the series."""
    reminder = get(con, reminder_id)
    if reminder is None:
        return False
    with con:
        if reminder.recurring and occurrence is not None:
            _log(con, reminder_id, occurrence, occurrence, "done")
            return True
        con.execute("UPDATE nova_reminders SET completed = 1, completed_at = ?, updated_at = ? WHERE id = ?",
                    (_stamp(), _stamp(), reminder_id))
        _log(con, reminder_id, reminder.occurrence, reminder.due, "done")
    return True


def delete(con, reminder_id):
    with con:
        cursor = con.execute("DELETE FROM nova_reminders WHERE id = ?", (int(reminder_id),))
        con.execute("DELETE FROM nova_reminder_events WHERE reminder_id = ?", (int(reminder_id),))
    return cursor.rowcount > 0


def _log(con, reminder_id, occurrence, alert_at, kind):
    """Record an event; returns False if the same event was already recorded."""
    cursor = con.execute(
        "INSERT OR IGNORE INTO nova_reminder_events (reminder_id, occurrence_at, alert_at, kind, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (reminder_id, fmt(occurrence), fmt(alert_at), kind, _stamp()))
    return cursor.rowcount == 1


# ==================== alerts ====================
@dataclass
class Alert:
    reminder_id: int
    title: str
    occurrence: datetime
    due: datetime
    missed: bool                  # came due while Nova wasn't running (or the PC was asleep)
    missed_count: int = 1         # occurrences of a repeating reminder that were missed
    rule: str | None = None
    anchor: str | None = None
    next_due: datetime | None = None
    snoozed: bool = False


def claim_due(con, now=None, grace=MISSED_GRACE_MINUTES, tz=None):
    """Claim every reminder that is due, exactly once, and return the alerts.

    Each claim is a conditional UPDATE (only one caller can win it) plus a
    unique alert record, so the same alert can never be shown twice, even by
    two Nova windows. A repeating reminder moves straight on to its next
    future occurrence; occurrences missed while Nova was closed are counted
    into a single alert instead of one alert each.
    """
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    alerts = []
    rows = con.execute(f"SELECT {SELECT} FROM nova_reminders WHERE completed = 0 AND notified_at IS NULL "
                       "AND due_at <= ? ORDER BY due_at, id", (fmt(now),)).fetchall()
    for row in rows:
        reminder = _reminder(row)
        with con:
            won = con.execute("UPDATE nova_reminders SET notified_at = ? WHERE id = ? AND notified_at IS NULL "
                              "AND due_at = ? AND completed = 0", (_stamp(), reminder.id, fmt(reminder.due)))
            if won.rowcount != 1:
                continue
            fresh = _log(con, reminder.id, reminder.occurrence, reminder.due, "alert")
            # Real elapsed time: a 02:30 alert on a spring-forward day (clocks jump
            # 02:00 -> 03:00) is on time at 03:00, not 30 minutes late.
            missed = elapsed_minutes(reminder.due, now, tz) > grace
            alert = Alert(reminder.id, reminder.title, reminder.occurrence, reminder.due, missed,
                          rule=reminder.rule, anchor=reminder.anchor, snoozed=reminder.due != reminder.occurrence)
            if reminder.recurring:
                following, count = next_occurrence(reminder.rule, reminder.anchor, reminder.occurrence), 1
                while following <= now and count < 10000:
                    if count <= 50:
                        _log(con, reminder.id, following, following, "missed")
                    following, count = next_occurrence(reminder.rule, reminder.anchor, following), count + 1
                alert.missed_count, alert.next_due = count, following
                alert.missed = alert.missed or count > 1
                con.execute("UPDATE nova_reminders SET due_at = ?, occurrence_at = ?, notified_at = NULL, "
                            "updated_at = ? WHERE id = ?", (fmt(following), fmt(following), _stamp(), reminder.id))
        if fresh:
            alerts.append(alert)
    return alerts


def alert_lines(alert, now=None):
    """Explanatory lines for an alert window."""
    now = now or datetime.now()
    lines = []
    if alert.rule and alert.missed_count > 1:
        lines.append(f"Missed {alert.missed_count} times while Nova wasn't running or the PC was asleep "
                     f"(first {when(alert.occurrence, now)}).")
    elif alert.missed:
        lines.append(f"Missed while Nova wasn't running or the PC was asleep. It was due {when(alert.due, now)}.")
    elif alert.snoozed:
        lines.append(f"Snoozed reminder (originally {when(alert.occurrence, now)}).")
    else:
        lines.append(f"Due {when(alert.due, now)}.")
    if alert.rule:
        lines.append(f"Repeats {describe_rule(alert.rule, alert.anchor)}. Next: {when(alert.next_due, now)}.")
    return lines


# ==================== views ====================
def grouped(con, now=None, completed_limit=30):
    """Reminders for the agenda view: overdue, by day, repeating and completed."""
    now = now or datetime.now()
    groups = {"overdue": [], "days": {}, "recurring": [], "completed": []}
    for row in con.execute(f"SELECT {SELECT} FROM nova_reminders WHERE completed = 0 ORDER BY due_at, id"):
        reminder = _reminder(row)
        if reminder.recurring:
            groups["recurring"].append(reminder)
        elif reminder.due <= now:
            groups["overdue"].append(reminder)
        else:
            groups["days"].setdefault(reminder.due.date(), []).append(reminder)
    groups["completed"] = [_reminder(r) for r in con.execute(
        f"SELECT {SELECT} FROM nova_reminders WHERE completed = 1 ORDER BY IFNULL(completed_at, due_at) DESC, "
        "id DESC LIMIT ?", (completed_limit,))]
    return groups


def active(con):
    return [_reminder(r) for r in con.execute(
        f"SELECT {SELECT} FROM nova_reminders WHERE completed = 0 ORDER BY due_at, id")]


def history(con, reminder_id, limit=20):
    return con.execute("SELECT occurrence_at, alert_at, kind, created_at FROM nova_reminder_events "
                       "WHERE reminder_id = ? ORDER BY id DESC LIMIT ?", (reminder_id, limit)).fetchall()


def saved_message(saved, now=None):
    """Chat reply after saving."""
    reminder = saved.reminder
    if not saved.created:
        return (f"You already have this reminder (#{reminder.id}): {reminder.title}, {reminder.describe(now)}. "
                "I didn't add a duplicate.")
    if reminder.recurring:
        text = (f"Repeating reminder #{reminder.id} saved: {reminder.title}, "
                f"{describe_rule(reminder.rule, reminder.anchor)}. First one: {when(reminder.due, now)}.")
    else:
        text = f"Reminder #{reminder.id} saved: {reminder.title}, {when(reminder.due, now)}."
    return text + (" Times use your PC's clock. I can only alert you while Nova is running "
                   "(the window or the tray icon); missed reminders appear when Nova next starts.")

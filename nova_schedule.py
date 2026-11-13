"""
Scheduled tasks, watches and notifications for Nova (Step 33). No GUI code.

There is one scheduler in Nova: the reminder engine (nova_reminders) and its once-every-2-seconds check in the
window. Step 33 extends it instead of adding another. Scheduled tasks use the same recurrence rules (daily,
weekdays, weekly, monthly, and now “every N minutes”), the same local wall-clock times, and the same
exactly-once claim (a conditional UPDATE plus a unique run record), and they're claimed in the same check.

  jobs           what to do (run an agent request, or just notify) and when:
                   time   once, or repeating (reminder rules)
                   file   a folder changes (new, changed or removed files matching a pattern); event-driven
                          (Windows change notifications), no polling
                   app    an approved app opens or closes (a cheap window check every 15 s, only while such a
                          watch exists)
                   web    a web page's text changes; only if online integrations are on, only the address you
                          gave, at most once an hour, with conditional requests
  runs           every execution: when (local time, with the time zone and UTC), why (the trigger), result,
                 error, the agent task it started; unique per job, occurrence and attempt, so nothing runs twice
  notifications  an inbox: each has a status, timestamp and the triggering condition; dismiss, snooze, inspect;
                 quiet hours hold back sound, speech and pop-ups

Missed runs (Nova closed, the PC asleep or off): a repeating task's missed occurrences are counted into one
run (“missed 3 times; ran once now”), or skipped, per task; anything older than the task's missed-run window is
skipped. Runs interrupted by a crash or shutdown are marked interrupted at the next start and retried within
the task's retry limit.

Permissions: an agent task runs through the Step 28 engine with the same tools, rules, limits, history and
audit as a typed request. Its plan is checked when the task is created and the user approves the tools and
folders it uses; a later run that would need another tool, a higher safety class or another folder doesn't
run and asks for review instead. Consequential steps always ask right before, as interactively; unanswered
means no.
"""
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nova_reminders as nr

log = logging.getLogger("nova.schedule")

ACTIONS = ("agent", "notify")
TRIGGERS = ("time", "file", "app", "web")
MISSED_POLICIES = ("run_once", "skip")
NOTIFY = ("always", "failures", "never")
APP_CHECK_SECONDS = 15
FILE_DEBOUNCE_SECONDS = 2.0
EVENT_COOLDOWN_SECONDS = 60          # a watch fires at most once a minute
WEB_MIN_MINUTES = 60
MAX_SNAPSHOT_FILES = 2000
MAX_WEB_BYTES = 2 * 1024 * 1024
DEFER_MINUTES = 2                    # another task was running: try again shortly

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('agent', 'notify')),
    request TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL CHECK (trigger IN ('time', 'file', 'app', 'web')),
    config TEXT NOT NULL DEFAULT '{}',
    rule TEXT,
    anchor TEXT,
    due_at TEXT,
    timezone TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'needs_review')),
    permissions TEXT NOT NULL DEFAULT '{}',
    missed_policy TEXT NOT NULL DEFAULT 'run_once',
    missed_window_hours INTEGER NOT NULL DEFAULT 12,
    max_retries INTEGER NOT NULL DEFAULT 1,
    retry_minutes INTEGER NOT NULL DEFAULT 10,
    notify TEXT NOT NULL DEFAULT 'always',
    speak INTEGER NOT NULL DEFAULT 0,
    state TEXT,
    retry_at TEXT,
    retry_occurrence TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    last_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due ON scheduled_jobs(status, due_at);
CREATE TABLE IF NOT EXISTS scheduled_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    occurrence_at TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    started_utc TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT,
    error TEXT,
    agent_run_id INTEGER,
    UNIQUE (job_id, occurrence_at, attempt)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_runs_job ON scheduled_runs(job_id, id);
CREATE TABLE IF NOT EXISTS nova_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',
    job_id INTEGER,
    run_id INTEGER,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'read', 'dismissed', 'snoozed')),
    snooze_until TEXT,
    delivered_at TEXT,
    quiet INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON nova_notifications(status, created_at);
"""


class ScheduleError(ValueError):
    """A task can't be saved or run as asked; the message is shown to the user."""


def ensure_schema(con):
    con.executescript(SCHEMA)
    con.commit()


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def tz_label(moment=None):
    """“India Standard Time, UTC+05:30” for the PC's time zone at a moment."""
    aware = (moment or datetime.now()).astimezone()
    offset = aware.utcoffset() or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    minutes = abs(int(offset.total_seconds() // 60))
    return f"{aware.tzname()}, UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def utc_stamp(moment, tz=None):
    aware = moment.replace(tzinfo=tz) if tz else moment.astimezone()
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


# ==================== jobs ====================
@dataclass
class Job:
    id: int
    name: str
    action: str
    request: str
    trigger: str
    config: dict
    rule: str | None
    anchor: str | None
    due: datetime | None
    timezone: str | None
    status: str
    permissions: dict
    missed_policy: str
    missed_window_hours: int
    max_retries: int
    retry_minutes: int
    notify: str
    speak: bool
    state: dict
    retry_at: datetime | None
    retry_occurrence: str | None
    attempts: int
    claimed_at: str | None
    last_run_at: str | None
    last_status: str | None
    last_error: str | None
    last_summary: str | None
    created_at: str
    updated_at: str

    def describe_trigger(self, now=None):
        if self.trigger == "time":
            if self.rule:
                text = nr.describe_rule(self.rule, self.anchor)
                return text[:1].upper() + text[1:]
            return f"Once, {nr.when(self.due, now)}" if self.due else "Once (done)"
        if self.trigger == "file":
            c = self.config
            events = "/".join(c.get("events") or ["created"])
            return f"When files matching {c.get('pattern') or '*'} are {events} in {c.get('folder')}"
        if self.trigger == "app":
            return f"When {self.config.get('app')} {self.config.get('when', 'opens')}"
        return f"When {self.config.get('url')} changes (checked {nr.describe_rule(self.rule, self.anchor)})"


_JOB = ("id, name, action, request, trigger, config, rule, anchor, due_at, timezone, status, permissions, "
        "missed_policy, missed_window_hours, max_retries, retry_minutes, notify, speak, state, retry_at, "
        "retry_occurrence, attempts, claimed_at, last_run_at, last_status, last_error, last_summary, created_at, "
        "updated_at")


def _loads(text):
    try:
        value = json.loads(text) if text else {}
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _job(row):
    return Job(row[0], row[1], row[2], row[3], row[4], _loads(row[5]), row[6], row[7],
               nr.parse_stamp(row[8]) if row[8] else None, row[9], row[10], _loads(row[11]), row[12], row[13],
               row[14], row[15], row[16], bool(row[17]), _loads(row[18]),
               nr.parse_stamp(row[19]) if row[19] else None, row[20], row[21], row[22], row[23], row[24], row[25],
               row[26], row[27], row[28])


def get_job(con, job_id):
    row = con.execute(f"SELECT {_JOB} FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
    return _job(row) if row else None


def list_jobs(con, status=None):
    where, args = ("WHERE status = ?", (status,)) if status else ("", ())
    return [_job(r) for r in con.execute(f"SELECT {_JOB} FROM scheduled_jobs {where} ORDER BY "
                                         "CASE status WHEN 'active' THEN 0 ELSE 1 END, due_at, id", args)]


def validate_config(trigger, config, rule=None):
    """The trigger's settings, checked: real folders, approved apps, http(s) pages, sane intervals."""
    config = dict(config or {})
    if trigger == "file":
        folder = str(config.get("folder") or "").strip()
        if not folder or not os.path.isdir(folder) or folder.startswith(("\\\\", "//")):
            raise ScheduleError("Choose an existing folder on this PC to watch.")
        try:
            import nova_actions
            nova_actions.check_not_protected(Path(folder).resolve(), None)
        except Exception as exc:
            if "protected" in str(exc) or "never" in str(exc):
                raise ScheduleError(str(exc)) from None
        events = [e for e in (config.get("events") or ["created"]) if e in ("created", "modified", "deleted")]
        pattern = str(config.get("pattern") or "*").strip() or "*"
        if any(ch in pattern for ch in "\\/:"):
            raise ScheduleError("The pattern is part of a file name, like *.pdf or invoice*.")
        return {"folder": str(Path(folder).resolve()), "pattern": pattern, "events": events or ["created"]}
    if trigger == "app":
        import nova_actions
        app = nova_actions.SWITCH_ALIASES.get(str(config.get("app") or "").lower().strip())
        if app is None or app == "nova":
            raise ScheduleError("Choose one of the apps Nova recognizes (Notepad, Calculator, Paint, VS Code, "
                                "File Explorer, browser).")
        when = config.get("when") if config.get("when") in ("opens", "closes") else "opens"
        return {"app": app, "when": when}
    if trigger == "web":
        import nova_browser
        try:
            url = nova_browser.check_url(config.get("url"))
        except nova_browser.BrowserError as exc:
            raise ScheduleError(str(exc)) from None
        if not url.startswith("https://"):
            raise ScheduleError("Nova only watches https pages.")
        kind, _, arg = str(rule or "").partition(":")
        if not (kind in ("daily", "weekly", "monthly") or (kind == "every" and int(arg or 0) >= WEB_MIN_MINUTES)):
            raise ScheduleError(f"Check a web page at most once every {WEB_MIN_MINUTES} minutes.")
        return {"url": url}
    return {}


def create_job(con, name, action, request, trigger, config=None, schedule=None, permissions=None, now=None,
               missed_policy="run_once", missed_window_hours=12, max_retries=1, retry_minutes=10, notify="always",
               speak=False):
    """Save a task. Time and web tasks need a schedule (nova_reminders.Schedule); agent tasks need the
    permissions the user approved for their checked plan."""
    now = now or datetime.now()
    name = " ".join(str(name or "").split())[:80]
    request = str(request or "").strip()[:2000]
    if not name:
        raise ScheduleError("Give the task a name.")
    if action not in ACTIONS or trigger not in TRIGGERS:
        raise ScheduleError("Unknown kind of task.")
    if not request:
        raise ScheduleError("Say what Nova should do (or the message to show).")
    if missed_policy not in MISSED_POLICIES or notify not in NOTIFY:
        raise ScheduleError("Unknown option.")
    if action == "agent" and not (permissions or {}).get("tools"):
        raise ScheduleError("Check the plan first, so you can approve what this task may do.")
    import nova_recall
    reason = nova_recall.sensitive(request)
    if reason:
        raise ScheduleError(f"The task text looks like {reason}; Nova doesn't store those.")
    config = validate_config(trigger, config, schedule.rule if schedule else None)
    if trigger in ("time", "web"):
        if schedule is None:
            raise ScheduleError("Choose when it runs.")
        if schedule.rule:
            nr.validate_rule(schedule.rule)
        elif schedule.due <= now:
            raise ScheduleError("Choose a time in the future.")
    due = schedule.due if schedule else None
    with con:
        job_id = con.execute(
            "INSERT INTO scheduled_jobs (name, action, request, trigger, config, rule, anchor, due_at, timezone, "
            "permissions, missed_policy, missed_window_hours, max_retries, retry_minutes, notify, speak, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, action, request, trigger, json.dumps(config), schedule.rule if schedule else None,
             schedule.anchor if schedule else None, nr.fmt(due) if due else None, tz_label(),
             json.dumps(permissions or {}), missed_policy, int(missed_window_hours), max(0, min(5, int(max_retries))),
             max(1, int(retry_minutes)), notify, int(bool(speak)), stamp(), stamp())).lastrowid
    log.info("Scheduled task %s created (%s, %s)", job_id, action, trigger)
    return get_job(con, job_id)


def update_job(con, job_id, now=None, **changes):
    """Edit a task. Changing what it does or its trigger needs its plan approved again (agent tasks)."""
    job = get_job(con, job_id)
    if job is None:
        raise ScheduleError("That task no longer exists.")
    now = now or datetime.now()
    fields, values = [], []
    if "schedule" in changes:
        schedule = changes.pop("schedule")
        if schedule.rule:
            nr.validate_rule(schedule.rule)
        elif schedule.due <= now:
            raise ScheduleError("Choose a time in the future.")
        fields += ["rule = ?", "anchor = ?", "due_at = ?", "retry_at = NULL", "attempts = 0"]
        values += [schedule.rule, schedule.anchor, nr.fmt(schedule.due)]
        if job.status == "done":
            fields.append("status = 'active'")
    if "config" in changes:
        config = validate_config(job.trigger, changes.pop("config"), changes.get("rule", job.rule))
        fields.append("config = ?")
        values.append(json.dumps(config))
        fields.append("state = NULL")
    if "request" in changes and job.action == "agent" and changes["request"].strip() != job.request \
            and "permissions" not in changes:
        raise ScheduleError("A changed request needs its plan checked and approved again.")
    for key in ("name", "request", "missed_policy", "missed_window_hours", "max_retries", "retry_minutes", "notify",
                "speak", "permissions"):
        if key in changes:
            value = changes[key]
            if key == "permissions":
                value = json.dumps(value or {})
                if job.status == "needs_review":
                    fields.append("status = 'active'")
            fields.append(f"{key} = ?")
            values.append(int(value) if key == "speak" else value)
    if fields:
        with con:
            con.execute(f"UPDATE scheduled_jobs SET {', '.join(fields)}, updated_at = ? WHERE id = ?",
                        (*values, stamp(), job_id))
    return get_job(con, job_id)


def set_status(con, job_id, status, now=None):
    """Pause, resume (active), or mark for review. Resuming a repeating task moves it to its next future time:
    nothing runs for the time it was paused."""
    job = get_job(con, job_id)
    if job is None:
        raise ScheduleError("That task no longer exists.")
    now = now or datetime.now()
    due = job.due
    if status == "active" and job.rule and due and due <= now:
        due = nr.next_occurrence(job.rule, job.anchor, now)
    if status == "active" and job.trigger == "time" and not job.rule and due and due <= now:
        raise ScheduleError("Its time has passed: edit it to choose a new time.")
    with con:
        con.execute("UPDATE scheduled_jobs SET status = ?, due_at = ?, updated_at = ? WHERE id = ?",
                    (status, nr.fmt(due) if due else None, stamp(), job_id))
    return get_job(con, job_id)


def delete_job(con, job_id):
    with con:
        removed = con.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,)).rowcount
        con.execute("DELETE FROM scheduled_runs WHERE job_id = ?", (job_id,))
    return bool(removed)


def history(con, job_id, limit=30):
    return [dict(zip(("id", "occurrence", "attempt", "reason", "status", "started", "started_utc", "finished",
                      "summary", "error", "agent_run_id"), r)) for r in con.execute(
        "SELECT id, occurrence_at, attempt, reason, status, started_at, started_utc, finished_at, summary, error, "
        "agent_run_id FROM scheduled_runs WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit))]


# ==================== claiming (exactly once) ====================
@dataclass
class Claim:
    job: Job
    run_id: int
    occurrence: str            # the scheduled time (local wall) or the event's time
    attempt: int
    reason: str                # why it runs: the schedule, a missed time, a retry, a file that appeared…
    missed_count: int = 0


def _start_run(con, job, occurrence, attempt, reason, now):
    try:
        run_id = con.execute(
            "INSERT INTO scheduled_runs (job_id, occurrence_at, attempt, reason, status, started_at, started_utc) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (job.id, occurrence, attempt, reason, now.strftime("%Y-%m-%d %H:%M:%S"), utc_stamp(now))).lastrowid
    except sqlite3.IntegrityError:
        return None                                         # this occurrence already ran (or is running)
    return run_id


def _skip_record(con, job, occurrence, reason, now, status="missed"):
    try:
        con.execute("INSERT INTO scheduled_runs (job_id, occurrence_at, attempt, reason, status, started_at, "
                    "started_utc, finished_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    (job.id, occurrence, reason, status, now.strftime("%Y-%m-%d %H:%M:%S"), utc_stamp(now),
                     now.strftime("%Y-%m-%d %H:%M:%S")))
    except sqlite3.IntegrityError:
        pass


def claim_due(con, now=None, tz=None, grace=nr.MISSED_GRACE_MINUTES):
    """Claim every time-based task (and web check) that is due, exactly once. Returns [Claim] to execute, and
    records skipped missed runs. Safe across restarts and several Nova windows: each claim is a conditional
    UPDATE and a unique run record."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    claims, skipped = [], []
    rows = con.execute(f"SELECT {_JOB} FROM scheduled_jobs WHERE status = 'active' AND claimed_at IS NULL AND "
                       "((due_at IS NOT NULL AND due_at <= ?) OR (retry_at IS NOT NULL AND retry_at <= ?)) "
                       "ORDER BY due_at, id", (nr.fmt(now), nr.fmt(now))).fetchall()
    for row in rows:
        job = _job(row)
        with con:
            if job.retry_at and job.retry_at <= now:                     # a retry of a failed occurrence
                won = con.execute("UPDATE scheduled_jobs SET claimed_at = ?, retry_at = NULL WHERE id = ? AND "
                                  "claimed_at IS NULL AND retry_at = ?", (stamp(), job.id, nr.fmt(job.retry_at)))
                if won.rowcount != 1:
                    continue
                attempt = job.attempts + 1
                run_id = _start_run(con, job, job.retry_occurrence or nr.fmt(job.retry_at), attempt,
                                    f"retry {attempt - 1} of {job.max_retries} after a failure", now)
                if run_id is None:
                    con.execute("UPDATE scheduled_jobs SET claimed_at = NULL WHERE id = ?", (job.id,))
                    continue
                claims.append(Claim(job, run_id, job.retry_occurrence or nr.fmt(job.retry_at), attempt,
                                    f"Retry {attempt - 1} of {job.max_retries} (the last try failed: "
                                    f"{job.last_error or 'unknown error'})"))
                continue
            if job.due is None or job.due > now:
                continue
            won = con.execute("UPDATE scheduled_jobs SET claimed_at = ? WHERE id = ? AND claimed_at IS NULL AND "
                              "due_at = ?", (stamp(), job.id, nr.fmt(job.due)))
            if won.rowcount != 1:
                continue
            late = nr.elapsed_minutes(job.due, now, tz)              # real minutes (DST-safe)
            following, count = job.due, 1
            if job.rule:
                following = nr.next_occurrence(job.rule, job.anchor, job.due)
                while following <= now and count < 100000:
                    following, count = nr.next_occurrence(job.rule, job.anchor, following), count + 1
            next_due = nr.fmt(following) if job.rule else None
            missed = late > grace
            occurrence = nr.fmt(job.due)
            if missed and (job.missed_policy == "skip" or late > job.missed_window_hours * 60):
                why = ("its missed-run setting is “skip”" if job.missed_policy == "skip" else
                       f"it was more than {job.missed_window_hours} hours late")
                _skip_record(con, job, occurrence, f"missed {count} time(s) while Nova wasn't running or the PC was "
                                                   f"asleep; skipped because {why}", now)
                con.execute("UPDATE scheduled_jobs SET due_at = ?, status = ?, claimed_at = NULL, attempts = 0, "
                            "last_status = 'missed', updated_at = ? WHERE id = ?",
                            (next_due, "active" if job.rule else "done", stamp(), job.id))
                skipped.append((job, count, why))
                continue
            reason = (f"Scheduled for {nr.when(job.due, now)}" if not missed else
                      f"Missed {count} time(s) while Nova wasn't running or the PC was asleep (first: "
                      f"{nr.when(job.due, now)}); run once now" if job.rule else
                      f"Missed while Nova wasn't running or the PC was asleep (due {nr.when(job.due, now)}); "
                      "run now")
            if job.trigger == "web":
                reason = f"Scheduled check of {job.config.get('url')}"
            run_id = _start_run(con, job, occurrence, 1, reason, now)
            if run_id is None:                                   # already ran: move on, never twice
                con.execute("UPDATE scheduled_jobs SET due_at = ?, claimed_at = NULL WHERE id = ?",
                            (next_due, job.id))
                continue
            con.execute("UPDATE scheduled_jobs SET due_at = ?, attempts = 0, updated_at = ? WHERE id = ?",
                        (next_due, stamp(), job.id))
            claims.append(Claim(job, run_id, occurrence, 1, reason, count if missed else 0))
    for job, count, why in skipped:
        notify(con, f"Skipped: {job.name}", f"It missed {count} scheduled time(s) while Nova wasn't running or the "
                                           f"PC was asleep, and was skipped because {why}.",
               "missed schedule", job_id=job.id, level="warning", now=now)
    return claims


def fire_event(con, job, reason, now=None):
    """An event (a file, an app) triggers a task: claimed exactly once, at most once per cooldown."""
    now = now or datetime.now()
    if job.status != "active":
        return None
    occurrence = now.strftime("%Y-%m-%d %H:%M:%S")
    with con:
        last = con.execute("SELECT MAX(started_utc) FROM scheduled_runs WHERE job_id = ?", (job.id,)).fetchone()[0]
        if last:
            since = (datetime.fromisoformat(utc_stamp(now)) - datetime.fromisoformat(last)).total_seconds()
            if since < EVENT_COOLDOWN_SECONDS:
                return None
        won = con.execute("UPDATE scheduled_jobs SET claimed_at = ? WHERE id = ? AND claimed_at IS NULL",
                          (stamp(), job.id))
        if won.rowcount != 1:
            return None
        run_id = _start_run(con, job, occurrence, 1, reason, now)
        if run_id is None:
            con.execute("UPDATE scheduled_jobs SET claimed_at = NULL WHERE id = ?", (job.id,))
            return None
    return Claim(job, run_id, occurrence, 1, reason)


def finish(con, claim, status, summary="", error=None, agent_run_id=None, now=None, notify_result=True):
    """Record the outcome; schedule a retry after a failure (within the task's limit); notify per preferences.
    Returns the notification id (or None)."""
    from nova_system import redact                     # Step 34: no secrets in stored results or errors
    now = now or datetime.now()
    job = get_job(con, claim.job.id) or claim.job
    finished = now.strftime("%Y-%m-%d %H:%M:%S")
    retry = None
    summary = redact(summary or "", 2000)
    error = redact(error, 1000) if error else None
    with con:
        con.execute("UPDATE scheduled_runs SET status = ?, finished_at = ?, summary = ?, error = ?, agent_run_id = ? "
                    "WHERE id = ?", (status, finished, (summary or "")[:2000], (error or None) and error[:1000],
                                     agent_run_id, claim.run_id))
        fields = ["claimed_at = NULL", "last_run_at = ?", "last_status = ?", "last_error = ?", "last_summary = ?",
                  "updated_at = ?"]
        values = [finished, status, error, (summary or "")[:500], stamp()]
        if status == "failed" and claim.attempt <= job.max_retries:
            retry = nr.ceil_minute(nr.shift(now, timedelta(minutes=job.retry_minutes)))
            fields += ["retry_at = ?", "retry_occurrence = ?", "attempts = ?"]
            values += [nr.fmt(retry), claim.occurrence, claim.attempt]
        elif status == "needs_review":
            fields.append("status = 'needs_review'")
        elif job.trigger == "time" and not job.rule and status in ("succeeded", "failed", "cancelled"):
            fields.append("status = 'done'")
        con.execute(f"UPDATE scheduled_jobs SET {', '.join(fields)} WHERE id = ?", (*values, job.id))
    if not notify_result or job.notify == "never" and status not in ("needs_review",):
        return None
    if job.notify == "failures" and status in ("succeeded",):
        return None
    if status == "succeeded" and claim.job.trigger == "web" and "no change" in (summary or ""):
        return None
    title = {"succeeded": f"Done: {job.name}", "failed": f"Failed: {job.name}",
             "needs_review": f"Needs your review: {job.name}", "cancelled": f"Stopped: {job.name}",
             "interrupted": f"Interrupted: {job.name}"}.get(status, f"{job.name}: {status}")
    body = (summary or "").strip()
    if error:
        body += ("\n\n" if body else "") + f"Problem: {error}"
    if retry:
        body += f"\n\nNova will try again {nr.when(retry, now)}."
    return notify(con, title, body, claim.reason, job_id=job.id, run_id=claim.run_id, now=now,
                  level="info" if status == "succeeded" else "warning")


def defer(con, claim, minutes=DEFER_MINUTES, now=None):
    """Another task is running: release the claim and try the same occurrence again in a few minutes
    (doesn't count as an attempt)."""
    now = now or datetime.now()
    later = nr.ceil_minute(nr.shift(now, timedelta(minutes=minutes)))
    with con:
        con.execute("DELETE FROM scheduled_runs WHERE id = ?", (claim.run_id,))
        con.execute("UPDATE scheduled_jobs SET claimed_at = NULL, retry_at = ?, retry_occurrence = ?, attempts = ? "
                    "WHERE id = ?", (nr.fmt(later), claim.occurrence, claim.attempt - 1, claim.job.id))


def recover(con, now=None):
    """At start-up: runs left “running” by a crash or shutdown are marked interrupted, their claims released,
    and retried within the task's limit. Returns how many."""
    now = now or datetime.now()
    rows = con.execute("SELECT r.id, r.job_id, r.occurrence_at, r.attempt, r.reason FROM scheduled_runs r "
                       "WHERE r.status = 'running'").fetchall()
    for run_id, job_id, occurrence, attempt, reason in rows:
        job = get_job(con, job_id)
        if job is None:
            continue
        claim = Claim(job, run_id, occurrence, attempt, reason)
        finish(con, claim, "failed", error="Nova was closed or stopped while this was running", now=now)
        with con:
            con.execute("UPDATE scheduled_runs SET status = 'interrupted' WHERE id = ?", (run_id,))
    with con:
        con.execute("UPDATE scheduled_jobs SET claimed_at = NULL WHERE claimed_at IS NOT NULL")
    return len(rows)


# ==================== permissions ====================
def permissions_for(plan):
    """What a checked plan needs: its tools, the most consequential safety class, and the folders it names."""
    import nova_agent as na
    import nova_tasks as nt
    tools = sorted({s.tool for s in plan.steps})
    worst = max((s.safety for s in plan.steps), key=lambda s: na.SAFETY_ORDER[s], default=na.READ_ONLY)
    folders = set()
    for s in plan.steps:
        for name in nt.PICK_PARAMS:
            value = (s.args or {}).get(name)
            if isinstance(value, str) and re.match(r"^[A-Za-z]:[\\/]", value):
                folders.add(value if os.path.isdir(value) else str(Path(value).parent))
    return {"tools": tools, "max_safety": worst, "folders": sorted(folders)}


def plan_allowed(plan, permissions):
    """None if a run's plan stays within what the user approved; otherwise why it doesn't."""
    import nova_agent as na
    tools = set(permissions.get("tools") or ())
    limit = na.SAFETY_ORDER.get(permissions.get("max_safety") or na.READ_ONLY, 0)
    extra = sorted({s.tool for s in plan.steps} - tools)
    if extra:
        return "the plan now uses " + ", ".join(t.replace("_", " ") for t in extra) + ", which you didn't approve"
    riskier = [s for s in plan.steps if na.SAFETY_ORDER[s.safety] > limit]
    if riskier:
        return f"“{riskier[0].description or riskier[0].tool}” {na.SAFETY_LABELS[riskier[0].safety]}, more than you " \
               "approved for this task"
    return None


def folder_allowed(folders, permissions):
    approved = [os.path.normcase(str(f)).rstrip("\\/") for f in permissions.get("folders") or ()]
    for folder in folders:
        text = os.path.normcase(str(folder)).rstrip("\\/")
        if not any(text == root or text.startswith(root + os.sep) for root in approved):
            return False
    return True


# ==================== notifications ====================
@dataclass
class Notification:
    id: int
    created_at: str
    title: str
    body: str
    reason: str
    level: str
    job_id: int | None
    run_id: int | None
    status: str
    snooze_until: str | None
    delivered_at: str | None
    quiet: bool


_NOTE = "id, created_at, title, body, reason, level, job_id, run_id, status, snooze_until, delivered_at, quiet"


def notify(con, title, body="", reason="", job_id=None, run_id=None, level="info", now=None):
    now = now or datetime.now()
    with con:
        return con.execute("INSERT INTO nova_notifications (created_at, title, body, reason, level, job_id, run_id) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (now.strftime("%Y-%m-%d %H:%M:%S"), str(title)[:200], str(body)[:3000],
                            str(reason)[:500], level, job_id, run_id)).lastrowid


def get_notification(con, note_id):
    row = con.execute(f"SELECT {_NOTE} FROM nova_notifications WHERE id = ?", (note_id,)).fetchone()
    return Notification(*row[:11], bool(row[11])) if row else None


def notifications(con, include_dismissed=False, limit=200):
    where = "" if include_dismissed else "WHERE status != 'dismissed'"
    return [Notification(*r[:11], bool(r[11])) for r in con.execute(
        f"SELECT {_NOTE} FROM nova_notifications {where} ORDER BY id DESC LIMIT ?", (limit,))]


def in_quiet_hours(settings, now=None):
    """Quiet hours (e.g. 22:00–07:00, across midnight) hold back sound, speech and pop-ups."""
    if not settings.get("quiet_hours_enabled"):
        return False
    now = now or datetime.now()
    try:
        start = datetime.strptime(settings.get("quiet_start") or "22:00", "%H:%M").time()
        end = datetime.strptime(settings.get("quiet_end") or "07:00", "%H:%M").time()
    except ValueError:
        return False
    t = now.time()
    return (start <= t < end) if start < end else (t >= start or t < end)


def due_for_delivery(con, settings, now=None):
    """Notifications to deliver now: new ones (outside quiet hours) and snoozed ones whose time has come. Each
    is marked delivered exactly once. During quiet hours, new ones are kept silently in the inbox."""
    now = now or datetime.now()
    quiet = in_quiet_hours(settings, now)
    stamp_now = now.strftime("%Y-%m-%d %H:%M:%S")
    delivered = []
    with con:
        con.execute("UPDATE nova_notifications SET status = 'new', delivered_at = NULL, snooze_until = NULL "
                    "WHERE status = 'snoozed' AND snooze_until <= ?", (stamp_now,))
        if quiet:
            con.execute("UPDATE nova_notifications SET quiet = 1 WHERE status = 'new' AND delivered_at IS NULL")
            return []
        rows = con.execute(f"SELECT {_NOTE} FROM nova_notifications WHERE status = 'new' AND delivered_at IS NULL "
                           "ORDER BY id").fetchall()
        for row in rows:
            if con.execute("UPDATE nova_notifications SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                           (stamp_now, row[0])).rowcount == 1:
                delivered.append(Notification(*row[:11], bool(row[11])))
    return delivered


def set_notification(con, note_id, status, snooze_minutes=None, now=None):
    now = now or datetime.now()
    until = (now + timedelta(minutes=snooze_minutes)).strftime("%Y-%m-%d %H:%M:%S") if snooze_minutes else None
    with con:
        con.execute("UPDATE nova_notifications SET status = ?, snooze_until = ? WHERE id = ?",
                    (status, until, note_id))


def unread_count(con):
    return con.execute("SELECT COUNT(*) FROM nova_notifications WHERE status = 'new'").fetchone()[0]


# ==================== watches ====================
def snapshot(folder, pattern="*", limit=MAX_SNAPSHOT_FILES):
    """{name: [size, mtime]} of the files directly in a folder matching the pattern (bounded)."""
    found = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if len(found) >= limit:
                    break
                if entry.is_file(follow_symlinks=False) and fnmatch.fnmatch(entry.name.lower(), pattern.lower()) \
                        and not entry.name.lower().endswith((".crdownload", ".part", ".tmp")) \
                        and not entry.name.startswith((".nova-copy-", "~$")):
                    info = entry.stat(follow_symlinks=False)
                    found[entry.name] = [info.st_size, int(info.st_mtime)]
    except OSError as exc:
        raise ScheduleError(f"The folder can't be read: {exc}") from None
    return found


def file_changes(before, after, events):
    """[(event, name)] between two snapshots, limited to the events asked for."""
    changes = []
    if "created" in events:
        changes += [("created", n) for n in sorted(set(after) - set(before))]
    if "deleted" in events:
        changes += [("deleted", n) for n in sorted(set(before) - set(after))]
    if "modified" in events:
        changes += [("modified", n) for n in sorted(set(before) & set(after)) if before[n] != after[n]]
    return changes


def check_file_job(con, job, now=None):
    """Compare the folder with the last snapshot; fire the task if matching files changed. The first look only
    records the snapshot (existing files don't count as new)."""
    config = job.config
    current = snapshot(config["folder"], config.get("pattern") or "*")
    previous = (job.state or {}).get("files")
    with con:
        con.execute("UPDATE scheduled_jobs SET state = ? WHERE id = ?", (json.dumps({"files": current}), job.id))
    if previous is None:
        return None
    changes = file_changes(previous, current, config.get("events") or ["created"])
    if not changes:
        return None
    shown = ", ".join(f"{name} ({event})" for event, name in changes[:5]) + (
        f" and {len(changes) - 5} more" if len(changes) > 5 else "")
    claim = fire_event(con, job, f"Files changed in {config['folder']}: {shown}", now)
    if claim is not None:
        claim.files = [str(Path(config["folder"]) / name) for event, name in changes if event != "deleted"]
    return claim


def check_app_job(con, job, running, now=None):
    """running: whether the app has a window now. Fires on the transition the task watches for."""
    before = (job.state or {}).get("running")
    with con:
        con.execute("UPDATE scheduled_jobs SET state = ? WHERE id = ?", (json.dumps({"running": running}), job.id))
    if before is None or before == running:
        return None
    if (job.config.get("when") == "opens") != running:
        return None
    return fire_event(con, job, f"{job.config['app'].capitalize()} {'opened' if running else 'closed'}", now)


def fetch_page(url, etag=None, modified=None, client=None, timeout=20):
    """(status, text, etag, last_modified) of an https page: conditional GET, no redirects elsewhere, size cap."""
    import httpx
    import nova_browser
    url = nova_browser.check_url(url)
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False,
                                    headers={"User-Agent": "Nova-desktop-assistant/1.0 (page change check)"})
    try:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                return 304, None, etag, modified
            if response.status_code != 200:
                raise ScheduleError(f"The page answered with error {response.status_code}.")
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_WEB_BYTES:
                    raise ScheduleError("The page is larger than Nova checks (2 MB).")
            return 200, body.decode(response.encoding or "utf-8", errors="replace"), response.headers.get("ETag"), \
                response.headers.get("Last-Modified")
    except httpx.HTTPError as exc:
        raise ScheduleError(f"The page couldn't be reached ({type(exc).__name__}).") from None
    finally:
        if own:
            client.close()


def page_text(html):
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(text.split())


def check_web_job(con, job, settings, fetch=fetch_page):
    """A scheduled check of one page: ('changed' | 'unchanged', summary). Only when online integrations are on."""
    import nova_online
    if not nova_online.enabled(settings):
        raise ScheduleError("Online integrations are off, so the page wasn't checked (Settings › Online).")
    state = job.state or {}
    status, html, etag, modified = fetch(job.config["url"], state.get("etag"), state.get("modified"))
    if status == 304:
        return "unchanged", "No change (the site said it hasn't changed)."
    digest = hashlib.sha256(page_text(html).encode("utf-8")).hexdigest()
    with con:
        con.execute("UPDATE scheduled_jobs SET state = ? WHERE id = ?",
                    (json.dumps({"hash": digest, "etag": etag, "modified": modified}), job.id))
    if state.get("hash") is None:
        return "unchanged", "No change: this was the first check, so Nova now knows the page's current text."
    if digest == state["hash"]:
        return "unchanged", "No change."
    return "changed", f"The text of {job.config['url']} changed since the last check."


class FileWatcher:
    """Waits for Windows' folder change notifications (no polling, no CPU while nothing changes) and calls
    on_change(job_id) once things settle. refresh(jobs) replaces the watched folders."""

    def __init__(self, on_change, debounce=FILE_DEBOUNCE_SECONDS):
        self.on_change, self.debounce = on_change, debounce
        self._thread, self._stop, self._wake = None, threading.Event(), threading.Event()
        self._jobs = {}

    def refresh(self, jobs):
        self._jobs = {j.id: j.config["folder"] for j in jobs if j.trigger == "file" and j.status == "active"}
        self._wake.set()
        if self._jobs and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="NovaFileWatch")
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(3)

    def _loop(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.FindFirstChangeNotificationW.restype = ctypes.c_void_p
        kernel32.FindFirstChangeNotificationW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint32]
        kernel32.FindNextChangeNotification.argtypes = [ctypes.c_void_p]
        kernel32.FindCloseChangeNotification.argtypes = [ctypes.c_void_p]
        kernel32.WaitForMultipleObjects.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                                    ctypes.c_uint32]
        invalid = ctypes.c_void_p(-1).value
        flags = 0x1 | 0x8 | 0x10                     # file names, size, last write
        while not self._stop.is_set():
            self._wake.clear()
            handles, owners = [], []
            for job_id, folder in list(self._jobs.items())[:60]:
                handle = kernel32.FindFirstChangeNotificationW(folder, False, flags)
                if handle and handle != invalid:
                    handles.append(handle)
                    owners.append(job_id)
                else:
                    log.warning("Can't watch %s for scheduled task %s", folder, job_id)
            if not handles:
                return
            array = (ctypes.c_void_p * len(handles))(*handles)
            pending = {}
            try:
                while not self._stop.is_set() and not self._wake.is_set():
                    result = kernel32.WaitForMultipleObjects(len(handles), array, False, 1000)
                    if 0 <= result < len(handles):
                        pending[owners[result]] = time.monotonic()
                        kernel32.FindNextChangeNotification(handles[result])
                    for job_id, at in list(pending.items()):
                        if time.monotonic() - at >= self.debounce:       # changes settled (downloads finish)
                            del pending[job_id]
                            try:
                                self.on_change(job_id)
                            except Exception as exc:
                                log.warning("File change handling failed: %s", exc)
            finally:
                for handle in handles:
                    kernel32.FindCloseChangeNotification(handle)


# ==================== chat commands ====================
_SCHEDULE = re.compile(r"^\s*(?:nova[,:]?\s+)?schedule\s+(?P<when>.+?)\s*:(?!\d)\s*(?P<what>.+?)\s*$", re.I | re.S)
_WATCH = re.compile(r"^\s*(?:nova[,:]?\s+)?(?:watch|monitor)\s+(?P<folder>[A-Za-z]:\\[^:]+?|downloads|documents|desktop|"
                    r"pictures|my\s+downloads|my\s+documents|my\s+desktop)\s+for\s+(?P<pattern>[^:]+?)"
                    r"(?:\s*:\s*(?P<what>.+?))?\s*$", re.I | re.S)
_WHEN_APP = re.compile(r"^\s*(?:nova[,:]?\s+)?(?:notify|tell|alert)\s+me\s+when\s+(?P<app>[a-z ]+?)\s+"
                       r"(?P<when>opens|is\s+opened|starts|closes|is\s+closed|quits)\s*[.!]?\s*$", re.I)
_LIST = re.compile(r"^\s*(?:nova[,:]?\s+)?(?:list|show)\s+(?:me\s+)?(?:my\s+)?(?:scheduled\s+tasks|schedules|"
                   r"automations|watches)\s*[.?!]?\s*$", re.I)


@dataclass
class Draft:
    """A task from a chat command, to review and approve in the editor before it's saved."""
    name: str
    action: str
    request: str
    trigger: str
    config: dict = field(default_factory=dict)
    schedule: object = None
    problem: str | None = None


def parse_command(text, now=None):
    """A Draft for “schedule every weekday at 9am: find invoices in downloads”, “watch downloads for *.pdf”,
    “notify me when notepad closes”; "list" for “list my automations”; None otherwise."""
    text = str(text or "")
    if _LIST.match(text):
        return "list"
    m = _SCHEDULE.match(text)
    if m:
        when, what = m.group("when"), m.group("what")
        parsed = nr.parse_reminder(f"remind me {when} to do it", now=now)
        action = "notify" if re.match(r"^(?:remind|notify|tell)\s+me\b", what, re.I) else "agent"
        request = re.sub(r"^(?:remind|notify|tell)\s+me\s+(?:to\s+|that\s+|about\s+)?", "", what, flags=re.I) \
            if action == "notify" else what
        draft = Draft(what[:60], action, request, "time")
        if parsed.kind == "ok":
            draft.schedule = parsed.schedule
        else:
            draft.problem = "Nova couldn't tell exactly when; choose the time below."
        return draft
    m = _WATCH.match(text)
    if m:
        folder = m.group("folder").strip()
        key = re.sub(r"^my\s+", "", folder.lower())
        if key in ("downloads", "documents", "desktop", "pictures"):
            try:
                import nova_actions
                folder = str(nova_actions.known_folder(key))
            except Exception:
                folder = str(Path.home() / key.title())
        pattern = m.group("pattern").strip().strip("\"'")
        pattern = pattern if any(c in pattern for c in "*?") else (
            f"*.{pattern.lstrip('.')}" if re.fullmatch(r"\.?[a-z0-9]{1,5}", pattern, re.I) else f"*{pattern}*")
        what = (m.group("what") or "").strip()
        return Draft(f"Watch {Path(folder).name} for {pattern}", "agent" if what else "notify",
                     what or f"New files matching {pattern} in {folder}", "file",
                     {"folder": folder, "pattern": pattern, "events": ["created"]})
    m = _WHEN_APP.match(text)
    if m:
        when = "closes" if re.search(r"clos|quit", m.group("when"), re.I) else "opens"
        app = m.group("app").strip()
        return Draft(f"When {app} {when}", "notify", f"{app.capitalize()} {'closed' if when == 'closes' else 'opened'}",
                     "app", {"app": app, "when": when})
    return None

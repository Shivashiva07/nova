"""
Upgrade a Nova database to the Step 32 memory schema, safely (Nova also does this by itself at start-up).

    python tools\\migrate_memory.py --check          # report what would change; changes nothing
    python tools\\migrate_memory.py                  # back up, upgrade, verify
    python tools\\migrate_memory.py --db D:\\copy\\nova_memory.db

What it does:
1. Checks the database (SQLite integrity check). A damaged database is never changed: restore a backup
   from Settings › Backup & restore, or start Nova, which offers to restore or set the file aside.
2. Copies the whole database to backups\\pre-memory-upgrade-<time>.db before changing anything.
3. Adds three columns to the memories table (confidence, source_ref, superseded_by, all with defaults) and
   four new tables (work_threads, work_entries, memory_blocklist, memory_changes). Nothing is dropped,
   rewritten or deleted; existing memories keep their text, dates, pins and flags.
4. Verifies the result (integrity check, same number of memories, chats and messages) and records schema
   version 25, unless the database is older than Nova's previous version, in which case Nova finishes the
   other upgrades at its next start.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nova_backup  # noqa: E402
import nova_memories  # noqa: E402
import nova_recall  # noqa: E402

NEW_TABLES = ("work_threads", "work_entries", "memory_blocklist", "memory_changes")


def pending_changes(con):
    columns = {r[1] for r in con.execute("PRAGMA table_info(memories)")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return ([c for c in ("confidence", "source_ref", "superseded_by") if columns and c not in columns],
            [t for t in NEW_TABLES if t not in tables], bool(columns))


def counts(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]       # names from a fixed list
            for t in ("memories", "sessions", "messages", "work_entries") if t in tables}


def migrate(db_path, check_only=False, backup_dir=None, out=print):
    """Returns 0 on success (or nothing to do), 1 if the database can't be upgraded."""
    db_path = Path(db_path)
    if not db_path.exists():
        out(f"No database at {db_path}. Nova creates one with the current schema when it starts.")
        return 0
    ok, message, version = nova_backup.check_database(db_path)
    if not ok:
        out(f"Not changed: {message}\nRestore a backup (Settings › Backup & restore), or start Nova, which offers to "
            "restore one or set the damaged file aside.")
        return 1
    if version > nova_backup.SCHEMA_VERSION:
        out(f"Not changed: the database is from a newer Nova (schema {version}).")
        return 1
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA foreign_keys = ON")
    try:
        columns, tables, has_memories = pending_changes(con)
        before = counts(con)
        out(f"Database: {db_path} (schema {version}); " + ", ".join(f"{n} {t}" for t, n in before.items()))
        if not has_memories:
            out("This database has no memories table yet; start Nova once to create it.")
            return 1
        if not columns and not tables:
            out("Already up to date: nothing to change.")
            if version < nova_backup.SCHEMA_VERSION and version >= 24:
                if not check_only:
                    nova_backup.stamp_schema_version(con)
            return 0
        out("Changes: " + "; ".join(filter(None, [
            f"add columns to memories: {', '.join(columns)}" if columns else "",
            f"create tables: {', '.join(tables)}" if tables else ""])))
        if check_only:
            out("--check: nothing was changed.")
            return 0
        backup_dir = Path(backup_dir or db_path.parent / "backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"pre-memory-upgrade-{datetime.now():%Y%m%d-%H%M%S}.db"
        nova_backup.snapshot_database(db_path, target)
        out(f"Backup: {target}")
        nova_memories.migrate(con, db_path)
        nova_recall.ensure_schema(con)
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        after = counts(con)
        unchanged = all(after.get(t) == n for t, n in before.items())
        if check != "ok" or not unchanged or pending_changes(con)[:2] != ([], []):
            out(f"The upgrade couldn't be verified (integrity: {check}; before {before}, after {after}). "
                f"Your data is in the backup: {target}")
            return 1
        if version >= 24:
            nova_backup.stamp_schema_version(con)
        else:
            out("This database is older than Nova's previous version: start Nova once to finish its other upgrades.")
        nova_recall.log_change(con, "migrate", "schema", nova_backup.SCHEMA_VERSION, "migration script")
        out(f"Done and verified: {', '.join(f'{n} {t}' for t, n in after.items())}; nothing was removed.")
        return 0
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=str(ROOT / "nova_memory.db"), help="the database file")
    parser.add_argument("--check", action="store_true", help="only report what would change")
    args = parser.parse_args(argv)
    return migrate(args.db, args.check)


if __name__ == "__main__":
    sys.exit(main())

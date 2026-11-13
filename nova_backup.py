"""
Backups, restore and database health checks for Nova. No GUI code.

A backup is a .novabackup file (a zip) containing:
  manifest.json      what's inside, checksums and record counts (validated on restore)
  nova_memory.db     a consistent snapshot of the database (SQLite online backup)
  nova_settings.json the settings file, if there is one

Restoring never trusts the file: member names are fixed (nothing is extracted
by name, so paths inside the archive can't escape), sizes are capped, the
manifest is checked field by field, checksums must match, the database must
pass SQLite's integrity check and contain Nova's core tables, and backups
from a newer Nova are refused. The current data is backed up first, and the
caller must pass confirmed=True after asking the user.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nova_system import NOVA_VERSION

log = logging.getLogger("nova.backup")

FORMAT = "nova-backup"
FORMAT_VERSION = 1
SCHEMA_VERSION = 26                 # stored in PRAGMA user_version once all migrations have run (26: Step 33)
SUFFIX = ".novabackup"
DB_MEMBER = "nova_memory.db"
SETTINGS_MEMBER = "nova_settings.json"
MANIFEST_MEMBER = "manifest.json"
MAX_DB_BYTES = 4 * 1024 ** 3
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SETTINGS_BYTES = 256 * 1024
AUTO_PREFIX = "auto-"
AUTO_KEEP = 7
REQUIRED_TABLES = {
    "sessions": {"id", "title", "created_at", "updated_at"},
    "messages": {"id", "session_id", "role", "content", "created_at"},
    "memories": {"id", "category", "content", "created_at", "updated_at"},
}
COUNTED_TABLES = ("sessions", "messages", "memories", "nova_notes", "nova_reminders", "doc_files", "task_runs",
                  "nova_projects", "work_entries", "scheduled_jobs")


class BackupError(Exception):
    """A backup can't be made or used; the message is shown to the user."""


@dataclass
class BackupInfo:
    path: Path
    created_at: str
    nova_version: str
    schema_version: int
    counts: dict = field(default_factory=dict)
    includes_settings: bool = False
    db_size: int = 0


# ==================== database health ====================
def schema_version(con):
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def stamp_schema_version(con):
    """Record that all migrations up to SCHEMA_VERSION have run (never lowers it)."""
    if schema_version(con) < SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        con.commit()


def check_database(db_path):
    """(ok, message, version). A missing file is fine (Nova creates one)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return True, "No database yet; Nova will create one.", 0
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            result = con.execute("PRAGMA quick_check").fetchone()[0]
            version = schema_version(con)
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        return False, f"The database file can't be read ({exc}).", 0
    if result != "ok":
        return False, f"The database failed its integrity check ({result}).", version
    if version > SCHEMA_VERSION:
        return True, (f"This database was last used by a newer version of Nova (schema {version}; this version "
                      f"understands {SCHEMA_VERSION})."), version
    return True, "OK", version


def table_counts(con):
    present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {name: int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])  # names from a fixed list
            for name in COUNTED_TABLES if name in present}


def _verify_database_file(path):
    """Raise BackupError unless path is a healthy Nova database. Returns (version, counts)."""
    try:
        con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise BackupError(f"The database in the backup is damaged ({check}).")
            for table, required in REQUIRED_TABLES.items():
                columns = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
                if not columns:
                    raise BackupError(f"The backup isn't a Nova database (no {table} table).")
                if not required <= columns:
                    raise BackupError(f"The backup's {table} table is missing columns: "
                                      f"{', '.join(sorted(required - columns))}.")
            version = schema_version(con)
            counts = table_counts(con)
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"The backup's database can't be read ({exc}).") from None
    if version > SCHEMA_VERSION:
        raise BackupError("This backup was made by a newer version of Nova and can't be restored by this one.")
    return version, counts


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ==================== creating backups ====================
def snapshot_database(db_path, target):
    """Copy the live database consistently (even while Nova is using it)."""
    source = sqlite3.connect(db_path, timeout=30)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


def default_name(now=None, prefix=""):
    return f"{prefix}nova-backup-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}{SUFFIX}"


def create_backup(db_path, destination, settings_path=None, now=None):
    """Write a backup. destination: a folder (a dated name is chosen) or a full file path.

    The archive is written to a temporary file and renamed into place, so a
    crash can't leave a half-written backup. Returns the backup's path.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise BackupError("There's no database to back up yet.")
    destination = Path(destination)
    target = destination / default_name(now) if destination.suffix.lower() != SUFFIX else destination
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="nova_backup_"))
    try:
        snapshot = work / DB_MEMBER
        try:
            snapshot_database(db_path, snapshot)
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"The current database can't be read, so it wasn't backed up ({exc}).") from None
        try:
            version, counts = _verify_database_file(snapshot)
        except BackupError as exc:
            raise BackupError(f"The current database looks damaged, so it wasn't backed up: {exc}") from None
        settings_bytes = None
        if settings_path and Path(settings_path).is_file():
            settings_bytes = Path(settings_path).read_bytes()[:MAX_SETTINGS_BYTES]
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app": "Nova",
            "nova_version": NOVA_VERSION,
            "schema_version": version,
            "created_at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
            "database": {"file": DB_MEMBER, "size": snapshot.stat().st_size, "sha256": _sha256_file(snapshot)},
            "settings": ({"file": SETTINGS_MEMBER, "sha256": hashlib.sha256(settings_bytes).hexdigest()}
                         if settings_bytes is not None else None),
            "counts": counts,
        }
        archive = work / "archive.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest, indent=2))
            zf.write(snapshot, DB_MEMBER)
            if settings_bytes is not None:
                zf.writestr(SETTINGS_MEMBER, settings_bytes)
        temp_target = target.with_name(f".{target.name}.tmp")
        shutil.copyfile(archive, temp_target)
        os.replace(temp_target, target)
        log.info("Backup written: %s (%d bytes)", target.name, target.stat().st_size)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def list_backups(folder):
    """Backups in a folder, newest first: [(path, modified datetime, size)]."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    items = []
    for path in folder.glob(f"*{SUFFIX}"):
        try:
            info = path.stat()
            items.append((path, datetime.fromtimestamp(info.st_mtime), info.st_size))
        except OSError:
            continue
    return sorted(items, key=lambda item: item[1], reverse=True)


def auto_backup(db_path, folder, settings_path=None, max_age_hours=24, keep=AUTO_KEEP, now=None):
    """Make a daily automatic backup if the newest one is older than max_age_hours; keep the last `keep`."""
    now = now or datetime.now()
    autos = [b for b in list_backups(folder) if b[0].name.startswith(AUTO_PREFIX)]
    if autos and (now - autos[0][1]).total_seconds() < max_age_hours * 3600:
        return None
    target = Path(folder) / default_name(now, AUTO_PREFIX)
    path = create_backup(db_path, target, settings_path, now)
    for old, _when, _size in [b for b in list_backups(folder) if b[0].name.startswith(AUTO_PREFIX)][keep:]:
        try:
            old.unlink()
        except OSError as exc:
            log.warning("Couldn't remove old automatic backup %s: %s", old.name, exc)
    return path


# ==================== validating and restoring ====================
def _require(condition, message):
    if not condition:
        raise BackupError(message)


def _validate_manifest(data):
    _require(isinstance(data, dict), "The backup's manifest isn't a JSON object.")
    _require(data.get("format") == FORMAT, "This file isn't a Nova backup.")
    _require(data.get("format_version") == FORMAT_VERSION and type(data.get("format_version")) is int,
             "This backup format isn't supported by this version of Nova.")
    version = data.get("schema_version")
    _require(type(version) is int and 0 <= version, "The backup's schema version is invalid.")
    _require(version <= SCHEMA_VERSION, "This backup was made by a newer version of Nova and can't be restored by this one.")
    created = data.get("created_at")
    _require(isinstance(created, str) and len(created) <= 40, "The backup's date is invalid.")
    try:
        datetime.fromisoformat(created)
    except ValueError:
        raise BackupError("The backup's date is invalid.") from None
    _require(isinstance(data.get("nova_version"), str) and len(data["nova_version"]) <= 20,
             "The backup's version field is invalid.")
    database = data.get("database")
    _require(isinstance(database, dict) and database.get("file") == DB_MEMBER, "The backup's database entry is invalid.")
    _require(type(database.get("size")) is int and 0 < database["size"] <= MAX_DB_BYTES,
             "The backup's database size is invalid.")
    _require(isinstance(database.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", database["sha256"]),
             "The backup's database checksum is invalid.")
    settings = data.get("settings")
    _require(settings is None or (isinstance(settings, dict) and settings.get("file") == SETTINGS_MEMBER and
                                  isinstance(settings.get("sha256"), str) and
                                  re.fullmatch(r"[0-9a-f]{64}", settings["sha256"])),
             "The backup's settings entry is invalid.")
    counts = data.get("counts", {})
    _require(isinstance(counts, dict) and len(counts) <= 50 and all(
        isinstance(k, str) and len(k) <= 64 and type(v) is int and v >= 0 for k, v in counts.items()),
        "The backup's record counts are invalid.")
    return data


def _extract(path, work):
    """Validate the archive and copy its known members into `work`. Returns (manifest, settings bytes)."""
    path = Path(path)
    _require(path.is_file(), "The backup file doesn't exist.")
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError(f"The file isn't a valid Nova backup ({exc}).") from None
    with archive:
        names = set(archive.namelist())
        _require(MANIFEST_MEMBER in names and DB_MEMBER in names, "The backup is incomplete (files are missing).")
        manifest_info = archive.getinfo(MANIFEST_MEMBER)
        _require(manifest_info.file_size <= MAX_MANIFEST_BYTES, "The backup's manifest is too large.")
        try:
            manifest = _validate_manifest(json.loads(archive.read(manifest_info).decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"The backup's manifest is damaged ({exc}).") from None
        db_info = archive.getinfo(DB_MEMBER)
        _require(db_info.file_size == manifest["database"]["size"], "The backup's database size doesn't match.")
        target = work / DB_MEMBER
        digest, written = hashlib.sha256(), 0
        try:
            with archive.open(db_info) as source, open(target, "wb") as out:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    written += len(block)
                    _require(written <= manifest["database"]["size"], "The backup's database is larger than declared.")
                    digest.update(block)
                    out.write(block)
        except (zipfile.BadZipFile, OSError, EOFError, zlib.error) as exc:
            raise BackupError(f"The backup is damaged ({exc}).") from None
        _require(digest.hexdigest() == manifest["database"]["sha256"],
                 "The backup's database doesn't match its checksum (the file is damaged or was altered).")
        settings_bytes = None
        if manifest["settings"] is not None:
            _require(SETTINGS_MEMBER in names, "The backup's settings file is missing.")
            info = archive.getinfo(SETTINGS_MEMBER)
            _require(info.file_size <= MAX_SETTINGS_BYTES, "The backup's settings file is too large.")
            settings_bytes = archive.read(info)
            _require(hashlib.sha256(settings_bytes).hexdigest() == manifest["settings"]["sha256"],
                     "The backup's settings don't match their checksum.")
    return manifest, settings_bytes


def inspect_backup(path):
    """Fully validate a backup without changing anything. Returns BackupInfo."""
    work = Path(tempfile.mkdtemp(prefix="nova_inspect_"))
    try:
        manifest, settings_bytes = _extract(path, work)
        version, counts = _verify_database_file(work / DB_MEMBER)
        return BackupInfo(Path(path), manifest["created_at"], manifest["nova_version"], version, counts,
                          settings_bytes is not None, manifest["database"]["size"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _settings_from_backup(settings_bytes):
    import nova_settings
    try:
        raw = json.loads(settings_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("The settings in the backup are damaged.") from None
    clean, _warnings = nova_settings.validate_settings(raw)
    return clean


@dataclass
class RestoreResult:
    info: BackupInfo
    safety_backup: Path | None
    damaged_copy: Path | None
    settings_restored: bool


def restore_backup(path, db_path, backup_folder, settings_path=None, confirmed=False):
    """Replace the live database (and settings) with a validated backup.

    Must be called with confirmed=True after the user agreed. Before
    anything changes, the current data is saved as a "pre-restore" backup
    (or, if it's too damaged to back up, copied aside as-is).
    """
    if not confirmed:
        raise BackupError("Restoring replaces all current data and must be confirmed first.")
    db_path = Path(db_path)
    work = Path(tempfile.mkdtemp(prefix="nova_restore_"))
    try:
        manifest, settings_bytes = _extract(path, work)
        version, counts = _verify_database_file(work / DB_MEMBER)
        settings = _settings_from_backup(settings_bytes) if settings_bytes is not None else None
        info = BackupInfo(Path(path), manifest["created_at"], manifest["nova_version"], version, counts,
                          settings is not None, manifest["database"]["size"])

        safety, damaged = None, None
        if db_path.exists():
            healthy, _message, _v = check_database(db_path)
            if healthy:
                safety = create_backup(db_path, Path(backup_folder) / default_name(prefix="pre-restore-"),
                                       settings_path)
            else:
                damaged = db_path.with_name(f"{db_path.stem}.damaged-{datetime.now():%Y%m%d-%H%M%S}.db")
                shutil.copy2(db_path, damaged)

        if damaged is None and db_path.exists():
            # Online copy into the live file: SQLite replaces every page in one transaction.
            source = sqlite3.connect(work / DB_MEMBER)
            try:
                target = sqlite3.connect(db_path, timeout=30)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        else:
            for suffix in ("-wal", "-shm"):
                stale = Path(str(db_path) + suffix)
                if stale.exists():
                    stale.unlink()
            _replace_file(work / DB_MEMBER, db_path)

        healthy, message, _v = check_database(db_path)
        if not healthy:
            raise BackupError(f"The restored database failed its check: {message}")
        if settings is not None and settings_path:
            import nova_settings
            nova_settings.save_settings(settings, settings_path)
        log.info("Restored backup %s (schema %s)", Path(path).name, version)
        return RestoreResult(info, safety, damaged, settings is not None)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _replace_file(source, target):
    """Copy source over target atomically (copy beside it, then rename)."""
    temp = Path(target).with_name(f".{Path(target).name}.restore.tmp")
    shutil.copyfile(source, temp)
    os.replace(temp, target)


def set_aside_damaged(db_path):
    """Move a damaged database (and its WAL files) aside so Nova can start fresh. Never deletes it."""
    db_path = Path(db_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.stem}.damaged-{stamp}.db")
    os.replace(db_path, target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            os.replace(side, Path(str(target) + suffix))
    log.warning("Damaged database moved aside to %s", target.name)
    return target

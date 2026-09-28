"""The core's shared state — one SQLite file instead of a JSON file per module.

Every durable module used to own a JSON file in the repo root, hold its own
lock, and rewrite the whole file on each change. That is safe inside one
process and nowhere else: the device hub, the job workers and the scheduler
ticker all write state, and a second process doing the same (a split core, a
restore running beside a live NORA) would silently lose updates to whichever
rename landed last. See NORA_DISTRIBUTED_PLAN.md §1, limitation 4.

So state that more than one writer touches lives here: `nora_core.db`, WAL
mode, one connection per thread, schema versioned with `PRAGMA user_version`.
Modules keep their public functions and swap storage underneath — nothing
outside `jobs`, `scheduler` and `task_ledger` should notice.

The legacy JSON files are imported once, on the first open that finds them,
and renamed to `*.json.migrated` so the import can never run twice and the
original is still there to read if something looks wrong.

`NORA_STORE_PATH` overrides the location. The test suite sets it, so tests
never touch the live database.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("nora.store")

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _ROOT / "nora_core.db"

# Each entry moves the schema from version i to i+1. Append only: a migration
# that has shipped is never edited, because a database somewhere is already
# past it.
_MIGRATIONS: list[str] = [
    # 1 — jobs, schedules, tasks (Phase 2a)
    """
    CREATE TABLE jobs (
        id           TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'answer',
        status       TEXT NOT NULL,
        result       TEXT NOT NULL DEFAULT '',
        error        TEXT NOT NULL DEFAULT '',
        created_at   REAL NOT NULL,
        started_at   REAL NOT NULL DEFAULT 0,
        finished_at  REAL NOT NULL DEFAULT 0,
        delivered    INTEGER NOT NULL DEFAULT 0,
        deliver      INTEGER NOT NULL DEFAULT 1,
        stale        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX jobs_status ON jobs(status);
    CREATE INDEX jobs_created ON jobs(created_at);

    CREATE TABLE schedules (
        id            TEXT PRIMARY KEY,
        spec          TEXT NOT NULL,
        what          TEXT NOT NULL,
        next_run      REAL NOT NULL,
        recurring     INTEGER NOT NULL DEFAULT 0,
        interval_sec  REAL NOT NULL DEFAULT 0,
        daily_at      TEXT,               -- JSON [hour, minute] or NULL
        weekday       INTEGER,
        enabled       INTEGER NOT NULL DEFAULT 1,
        created_at    REAL NOT NULL,
        last_run      REAL NOT NULL DEFAULT 0,
        run_count     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX schedules_due ON schedules(enabled, next_run);

    CREATE TABLE tasks (
        id                TEXT PRIMARY KEY,
        title             TEXT NOT NULL,
        status            TEXT NOT NULL,
        notes             TEXT NOT NULL DEFAULT '',
        log               TEXT NOT NULL DEFAULT '[]',   -- JSON list
        associated_files  TEXT NOT NULL DEFAULT '[]',   -- JSON list
        commands          TEXT NOT NULL DEFAULT '[]',   -- JSON list
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL,
        closed_at         REAL
    );
    CREATE INDEX tasks_updated ON tasks(updated_at);
    """,
    # 2 — delivery targets and the device hub (Phase 2b–d)
    """
    ALTER TABLE jobs ADD COLUMN device TEXT NOT NULL DEFAULT 'local';
    ALTER TABLE schedules ADD COLUMN device TEXT NOT NULL DEFAULT 'local';

    CREATE TABLE devices (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        platform     TEXT NOT NULL,
        public_key   BLOB NOT NULL,          -- SubjectPublicKeyInfo DER, P-256
        paired_at    REAL NOT NULL,
        approved_at  REAL,                   -- NULL until the user approves it
        revoked_at   REAL,
        last_seen    REAL
    );

    CREATE TABLE pairing_codes (
        code_hash    TEXT PRIMARY KEY,       -- sha256 of the code; the code is never stored
        created_at   REAL NOT NULL,
        expires_at   REAL NOT NULL,
        used_at      REAL
    );

    CREATE TABLE invocations (
        id           TEXT PRIMARY KEY,
        device_id    TEXT NOT NULL,
        capability   TEXT NOT NULL,
        params       TEXT NOT NULL,          -- JSON
        origin       TEXT NOT NULL,
        turn_id      TEXT NOT NULL DEFAULT '',
        tier         INTEGER NOT NULL,
        status       TEXT NOT NULL,          -- sent | ok | error
        confirmed_by TEXT NOT NULL DEFAULT '',
        result       TEXT,                   -- JSON
        error_code   TEXT,
        created_at   REAL NOT NULL,
        finished_at  REAL
    );
    CREATE INDEX invocations_device ON invocations(device_id, created_at);
    """,
]

_local = threading.local()
_init_lock = threading.Lock()
_initialised: set[str] = set()


def path() -> Path:
    override = os.environ.get("NORA_STORE_PATH", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_PATH


def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: autocommit, with explicit BEGIN in `transaction()`.
    # Python's implicit transactions open lazily and commit at surprising
    # moments, which is the wrong default for state shared across threads.
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None,
                           check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current, len(_MIGRATIONS)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read inside the write lock: another process may have migrated
            # between the check above and here.
            if conn.execute("PRAGMA user_version").fetchone()[0] != version:
                conn.execute("ROLLBACK")
                continue
            for statement in _split(_MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version + 1}")
            conn.execute("COMMIT")
            logger.info("Store migrated to schema v%d", version + 1)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split(script: str) -> list[str]:
    """Split a migration into statements. `executescript` would commit the
    surrounding transaction, so each statement runs on its own instead.
    `--` comments are dropped first so a semicolon inside one cannot split a
    statement. Migrations contain no string literals with `--` or `;`."""
    code = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    return [s.strip() for s in code.split(";") if s.strip()]


def connect() -> sqlite3.Connection:
    """This thread's connection to the store, migrated and ready.

    One connection per thread (SQLite connections are not thread-safe), cached
    per path so a test that repoints `NORA_STORE_PATH` gets a fresh one.
    """
    db_path = path()
    key = str(db_path)
    cached = getattr(_local, "conns", None)
    if cached is None:
        cached = _local.conns = {}
    conn = cached.get(key)
    if conn is not None:
        return conn
    conn = _open(db_path)
    with _init_lock:
        if key not in _initialised:
            _migrate(conn)
            _import_legacy(conn)
            _initialised.add(key)
    cached[key] = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A write transaction. `BEGIN IMMEDIATE` takes the write lock up front, so
    a read-modify-write inside it cannot interleave with another writer."""
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def reset_for_tests() -> None:
    """Delete every row. Tests only, and only against a non-default path."""
    if path() == _DEFAULT_PATH:
        raise RuntimeError("refusing to wipe the live store; set NORA_STORE_PATH")
    with transaction() as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            conn.execute(f"DELETE FROM {name}")


# ── One-time import of the legacy JSON files ─────────────────────────────────

def _legacy_dir() -> Path:
    """Where the old JSON files live: beside the database."""
    return path().parent


def _import_legacy(conn: sqlite3.Connection) -> None:
    for filename, importer in (
        ("nora_jobs.json", _import_jobs),
        ("nora_schedules.json", _import_schedules),
        ("nora_tasks.json", _import_tasks),
    ):
        src = _legacy_dir() / filename
        if not src.exists():
            continue
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except Exception as e:
            # Leave it where it is: a file we cannot read is one a human should
            # look at, not one to rename out of sight.
            logger.warning("Legacy %s unreadable, not imported: %s", filename, e)
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = importer(conn, raw)
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            logger.warning("Legacy %s import failed, left in place: %s", filename, e)
            continue
        src.replace(src.with_name(src.name + ".migrated"))
        logger.info("Imported %d rows from %s into the store", count, filename)


def _import_jobs(conn: sqlite3.Connection, raw: Any) -> int:
    rows = raw.get("jobs", []) if isinstance(raw, dict) else []
    n = 0
    for j in rows:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO jobs (id, title, kind, status, result, error,"
            " created_at, started_at, finished_at, delivered, deliver, stale)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (j["id"], j.get("title", ""), j.get("kind", "answer"),
             j.get("status", "failed"), j.get("result", ""), j.get("error", ""),
             float(j.get("created_at") or 0), float(j.get("started_at") or 0),
             float(j.get("finished_at") or 0), int(bool(j.get("delivered"))),
             int(bool(j.get("deliver", True))), int(bool(j.get("stale")))),
        )
        n += 1
    return n


def _import_schedules(conn: sqlite3.Connection, raw: Any) -> int:
    rows = raw.get("schedules", []) if isinstance(raw, dict) else []
    n = 0
    for s in rows:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        daily = s.get("daily_at")
        conn.execute(
            "INSERT OR IGNORE INTO schedules (id, spec, what, next_run, recurring,"
            " interval_sec, daily_at, weekday, enabled, created_at, last_run, run_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s.get("spec", ""), s.get("what", ""),
             float(s.get("next_run") or 0), int(bool(s.get("recurring"))),
             float(s.get("interval_sec") or 0),
             json.dumps(daily) if daily is not None else None,
             s.get("weekday"), int(bool(s.get("enabled", True))),
             float(s.get("created_at") or 0), float(s.get("last_run") or 0),
             int(s.get("run_count") or 0)),
        )
        n += 1
    return n


def _import_tasks(conn: sqlite3.Connection, raw: Any) -> int:
    rows = raw.values() if isinstance(raw, dict) else []
    n = 0
    for t in rows:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, title, status, notes, log,"
            " associated_files, commands, created_at, updated_at, closed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", ""), t.get("status", "open"),
             t.get("notes", "") or "", json.dumps(t.get("log") or []),
             json.dumps(t.get("associated_files") or []),
             json.dumps(t.get("commands") or []),
             float(t.get("created_at") or 0), float(t.get("updated_at") or 0),
             t.get("closed_at")),
        )
        n += 1
    return n

r"""SQLite-backed durable queue.

Status lifecycle:  pending -> processing -> done
                                        \-> pending (retry, with backoff)
                                        \-> failed (attempts exhausted)
"""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ntfy_msg_id     TEXT    NOT NULL UNIQUE,
    topic           TEXT    NOT NULL,
    raw_text        TEXT    NOT NULL,
    hint_title      TEXT,
    hint_tags       TEXT,
    hint_priority   INTEGER,
    project_id      TEXT,
    tududi_task_id  TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    received_at     INTEGER NOT NULL,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    claimed_at      INTEGER,
    completed_at    INTEGER,
    result_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_claim
    ON queue(status, next_attempt_at, received_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


# ---------- cursor ----------

def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------- ingest ----------

def enqueue(conn, *, ntfy_msg_id, topic, raw_text, hint_title,
            hint_tags, hint_priority, project_id):
    """Returns the row id, or None if this message was already seen."""
    try:
        cur = conn.execute(
            "INSERT INTO queue (ntfy_msg_id, topic, raw_text, hint_title, "
            "hint_tags, hint_priority, project_id, received_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ntfy_msg_id, topic, raw_text, hint_title,
             json.dumps(hint_tags or []), hint_priority,
             project_id, int(time.time())),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def attach_task(conn, row_id, task_id):
    conn.execute("UPDATE queue SET tududi_task_id=? WHERE id=?", (task_id, row_id))


# ---------- worker ----------

def reclaim_stale(conn, stale_after: int) -> int:
    cutoff = int(time.time()) - stale_after
    cur = conn.execute(
        "UPDATE queue SET status='pending', claimed_at=NULL "
        "WHERE status='processing' AND claimed_at < ?",
        (cutoff,),
    )
    return cur.rowcount


def claim_one(conn):
    """Atomically claim the oldest eligible pending row."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM queue WHERE status='pending' AND next_attempt_at <= ? "
            "ORDER BY received_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE queue SET status='processing', claimed_at=? WHERE id=?",
            (now, row["id"]),
        )
        conn.execute("COMMIT")
        # Return post-update state, not the row as it was read.
        claimed = dict(row)
        claimed["status"] = "processing"
        claimed["claimed_at"] = now
        return claimed
    except Exception:
        conn.execute("ROLLBACK")
        raise


def mark_done(conn, row_id, result: dict):
    conn.execute(
        "UPDATE queue SET status='done', completed_at=?, result_json=?, "
        "last_error=NULL WHERE id=?",
        (int(time.time()), json.dumps(result), row_id),
    )


def mark_failed(conn, row_id, error: str, max_attempts: int, backoff_base: int):
    """Retry with exponential backoff, or give up. Returns the new status."""
    row = conn.execute("SELECT attempts FROM queue WHERE id=?", (row_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE queue SET status='failed', attempts=?, last_error=?, "
            "completed_at=? WHERE id=?",
            (attempts, error[:2000], int(time.time()), row_id),
        )
        return "failed"
    delay = backoff_base * (2 ** (attempts - 1))
    conn.execute(
        "UPDATE queue SET status='pending', attempts=?, last_error=?, "
        "next_attempt_at=?, claimed_at=NULL WHERE id=?",
        (attempts, error[:2000], int(time.time()) + delay, row_id),
    )
    return "pending"


def stats(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM queue GROUP BY status"
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}

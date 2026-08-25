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

-- Planning pipeline: reads existing tududi tasks (tag-triggered) rather than
-- capturing new ones, and needs an `awaiting_input` async-wait state the
-- capture queue above has no use for -- kept as its own table rather than
-- new columns on `queue`.
CREATE TABLE IF NOT EXISTS plan_queue (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tududi_task_id       TEXT    NOT NULL,
    project_id           TEXT,
    task_name            TEXT,
    task_note            TEXT,
    status               TEXT    NOT NULL DEFAULT 'pending',
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    discovered_at        INTEGER NOT NULL,
    next_attempt_at      INTEGER NOT NULL DEFAULT 0,
    claimed_at           INTEGER,
    completed_at         INTEGER,
    result_json          TEXT,
    conversation_json    TEXT,
    clarification_round  INTEGER NOT NULL DEFAULT 0,
    correlation_token    TEXT,
    question_text        TEXT,
    awaiting_since       INTEGER,
    reply_text           TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_queue_claim
    ON plan_queue(status, next_attempt_at, discovered_at);
-- Only one *active* row per task; a done/failed task can be re-queued by
-- re-adding the trigger tag, which starts a fresh row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_queue_active_task
    ON plan_queue(tududi_task_id) WHERE status IN ('pending','processing','awaiting_input');
CREATE INDEX IF NOT EXISTS idx_plan_queue_correlation
    ON plan_queue(correlation_token) WHERE correlation_token IS NOT NULL;
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


# ---------- planner ----------
# Deliberately parallel to, not shared with, the queue-table functions above --
# same explicit-over-generic style as the rest of this file.

def plan_enqueue(conn, *, tududi_task_id, project_id, task_name, task_note):
    """Returns the row id, or None if an active row for this task already exists."""
    try:
        cur = conn.execute(
            "INSERT INTO plan_queue (tududi_task_id, project_id, task_name, "
            "task_note, discovered_at) VALUES (?,?,?,?,?)",
            (tududi_task_id, project_id, task_name, task_note, int(time.time())),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def plan_claim_one(conn):
    """Atomically claim the oldest eligible pending row."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM plan_queue WHERE status='pending' AND next_attempt_at <= ? "
            "ORDER BY discovered_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE plan_queue SET status='processing', claimed_at=? WHERE id=?",
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


def plan_mark_done(conn, row_id, result: dict):
    conn.execute(
        "UPDATE plan_queue SET status='done', completed_at=?, result_json=?, "
        "last_error=NULL WHERE id=?",
        (int(time.time()), json.dumps(result), row_id),
    )


def plan_mark_failed(conn, row_id, error: str, max_attempts: int, backoff_base: int):
    """Retry with exponential backoff, or give up. Returns the new status."""
    row = conn.execute("SELECT attempts FROM plan_queue WHERE id=?", (row_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE plan_queue SET status='failed', attempts=?, last_error=?, "
            "completed_at=? WHERE id=?",
            (attempts, error[:2000], int(time.time()), row_id),
        )
        return "failed"
    delay = backoff_base * (2 ** (attempts - 1))
    conn.execute(
        "UPDATE plan_queue SET status='pending', attempts=?, last_error=?, "
        "next_attempt_at=?, claimed_at=NULL WHERE id=?",
        (attempts, error[:2000], int(time.time()) + delay, row_id),
    )
    return "pending"


def plan_reclaim_stale(conn, stale_after: int) -> int:
    """Only 'processing' rows are ever reclaimed -- 'awaiting_input' is an
    intentional wait state, not a stuck one."""
    cutoff = int(time.time()) - stale_after
    cur = conn.execute(
        "UPDATE plan_queue SET status='pending', claimed_at=NULL "
        "WHERE status='processing' AND claimed_at < ?",
        (cutoff,),
    )
    return cur.rowcount


def plan_park_awaiting(conn, row_id, *, correlation_token, question_text, conversation_json):
    conn.execute(
        "UPDATE plan_queue SET status='awaiting_input', correlation_token=?, "
        "question_text=?, conversation_json=?, awaiting_since=?, claimed_at=NULL "
        "WHERE id=?",
        (correlation_token, question_text, conversation_json, int(time.time()), row_id),
    )


def plan_find_awaiting_by_token(conn, token):
    row = conn.execute(
        "SELECT * FROM plan_queue WHERE status='awaiting_input' AND correlation_token=?",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def plan_find_sole_awaiting(conn):
    """Returns the row only if exactly one is awaiting_input -- lets a bare
    reply resolve unambiguously in the common case of a single open question."""
    rows = conn.execute(
        "SELECT * FROM plan_queue WHERE status='awaiting_input'"
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def plan_resume_with_reply(conn, row_id, reply_text, conversation_json):
    conn.execute(
        "UPDATE plan_queue SET status='pending', reply_text=?, conversation_json=?, "
        "clarification_round=clarification_round+1, next_attempt_at=0, "
        "correlation_token=NULL, question_text=NULL, awaiting_since=NULL, "
        "claimed_at=NULL WHERE id=?",
        (reply_text, conversation_json, row_id),
    )


def plan_stale_awaiting(conn, reminder_after: int):
    """Rows unanswered longer than reminder_after seconds. 0 disables (returns [])."""
    if not reminder_after:
        return []
    cutoff = int(time.time()) - reminder_after
    rows = conn.execute(
        "SELECT * FROM plan_queue WHERE status='awaiting_input' AND awaiting_since < ?",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def plan_touch_awaiting(conn, row_id):
    conn.execute("UPDATE plan_queue SET awaiting_since=? WHERE id=?",
                 (int(time.time()), row_id))


def plan_stats(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM plan_queue GROUP BY status"
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}

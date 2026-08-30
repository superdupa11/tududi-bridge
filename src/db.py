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

-- Execution pipeline: picks up tasks already scoped by the planner above and
-- runs them through the Ollama tool-calling agent in agent.py. Own queue
-- table for the same reason plan_queue is separate from queue -- different
-- lifecycle (a workspace clone + branch instead of a note field). DOES have
-- an awaiting_input state, like plan_queue -- the agent can pause mid-run on
-- a genuine question or a push/build approval. Unlike plan_queue, there's no
-- correlation_token: every row gets its own freshly-generated ntfy topic
-- (ntfy_topic), so any reply on that topic unambiguously belongs to this row.
CREATE TABLE IF NOT EXISTS exec_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tududi_task_id    TEXT    NOT NULL,
    project_id        TEXT,
    task_name         TEXT,
    plan_json         TEXT,
    branch            TEXT,
    workspace_dir     TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    discovered_at     INTEGER NOT NULL,
    next_attempt_at   INTEGER NOT NULL DEFAULT 0,
    claimed_at        INTEGER,
    completed_at      INTEGER,
    result_json       TEXT,
    transcript_json   TEXT,
    ntfy_topic        TEXT,
    park_kind         TEXT,
    question_text     TEXT,
    pending_command   TEXT,
    awaiting_since    INTEGER,
    reply_text        TEXT,
    conversation_json TEXT,
    elapsed_seconds   REAL    NOT NULL DEFAULT 0,
    steps_used        INTEGER NOT NULL DEFAULT 0,
    run_backend       TEXT,
    -- A plan's `chunks` (see planning.py) execute one exec_queue row at a
    -- time -- chunk_index/chunk_count describe this row's position, so
    -- executor.py knows whether to branch fresh off base (chunk_index==0)
    -- or continue an existing branch, and whether finishing this row
    -- should enqueue the next chunk or finalize the task. A plan without
    -- chunks (or predating this feature) is just chunk_count=1.
    chunk_index       INTEGER NOT NULL DEFAULT 0,
    chunk_count       INTEGER NOT NULL DEFAULT 1,
    -- JSON array of tududi subtask uids, index-aligned with plan["chunks"] --
    -- created once (all N at once) when chunk 0 starts, carried forward
    -- unchanged to every later chunk's row the same way branch/workspace_dir/
    -- ntfy_topic already are. Deliberately not folded into plan_json, which
    -- stays purely the planner's own output.
    subtask_uids_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_queue_claim
    ON exec_queue(status, next_attempt_at, discovered_at);
-- Only one *active* row per task, same as plan_queue -- a done/failed task
-- can be re-queued by re-adding the trigger tag.
CREATE UNIQUE INDEX IF NOT EXISTS idx_exec_queue_active_task
    ON exec_queue(tududi_task_id) WHERE status IN ('pending','processing','awaiting_input');
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS above only helps a fresh DB -- an existing
    # queue.db predating chunked execution needs these columns added by hand.
    for ddl in ("ALTER TABLE exec_queue ADD COLUMN chunk_index INTEGER NOT NULL DEFAULT 0",
               "ALTER TABLE exec_queue ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 1",
               "ALTER TABLE exec_queue ADD COLUMN subtask_uids_json TEXT"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
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


def plan_latest_result_for_task(conn, tududi_task_id):
    """The structured plan dict finalize_plan() stored under result_json['plan']
    for the most recent completed plan_queue row for this task, or None if
    there isn't one -- how the executor gets the plan without re-parsing the
    rendered note's prose."""
    row = conn.execute(
        "SELECT result_json FROM plan_queue WHERE tududi_task_id=? AND status='done' "
        "ORDER BY completed_at DESC LIMIT 1",
        (str(tududi_task_id),),
    ).fetchone()
    if not row or not row["result_json"]:
        return None
    return json.loads(row["result_json"]).get("plan")


# ---------- executor ----------
# Deliberately parallel to, not shared with, the plan_* functions above --
# same explicit-over-generic style as the rest of this file.

def exec_enqueue(conn, *, tududi_task_id, project_id, task_name, plan_json,
                 chunk_index=0, chunk_count=1, branch=None, workspace_dir=None, ntfy_topic=None,
                 run_backend=None, subtask_uids_json=None):
    """Returns the row id, or None if an active row for this task already exists.

    chunk_index/chunk_count/branch/workspace_dir/ntfy_topic/run_backend/
    subtask_uids_json let executor.py enqueue chunk N+1 of a chunked plan
    pre-seeded with everything chunk 0 already resolved, so process() can
    detect continuation (see chunk_index>0 there) and skip re-cloning/
    re-branching/re-announcing/re-resolving the Mac-vs-docker backend/
    re-creating tududi subtasks -- the exec_queue active-row unique index
    (idx_exec_queue_active_task) is what makes this safe: this insert can
    only succeed once the prior chunk's row has reached a terminal status."""
    try:
        cur = conn.execute(
            "INSERT INTO exec_queue (tududi_task_id, project_id, task_name, plan_json, "
            "discovered_at, chunk_index, chunk_count, branch, workspace_dir, ntfy_topic, "
            "run_backend, subtask_uids_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tududi_task_id, project_id, task_name, plan_json, int(time.time()),
             chunk_index, chunk_count, branch, workspace_dir, ntfy_topic, run_backend,
             subtask_uids_json),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def exec_set_subtask_uids(conn, row_id, subtask_uids_json: str):
    conn.execute("UPDATE exec_queue SET subtask_uids_json=? WHERE id=?",
                (subtask_uids_json, row_id))


def exec_claim_one(conn):
    """Atomically claim the oldest eligible pending row."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM exec_queue WHERE status='pending' AND next_attempt_at <= ? "
            "ORDER BY discovered_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE exec_queue SET status='processing', claimed_at=? WHERE id=?",
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


def exec_mark_done(conn, row_id, result: dict, *, branch=None, workspace_dir=None,
                   transcript=None, steps_used=None, elapsed_seconds=None):
    """steps_used/elapsed_seconds are optional refreshes of the same-named
    columns exec_park_awaiting() writes during a pause -- without them here,
    those columns would stay stuck at their value from the last park instead
    of reflecting the run's actual final total (still correct either way
    inside result_json, which is what the rendered note uses; this is purely
    for anyone inspecting exec_queue directly)."""
    sets = ["status='done'", "completed_at=?", "result_json=?", "branch=?", "workspace_dir=?",
           "transcript_json=?", "last_error=NULL"]
    params = [int(time.time()), json.dumps(result), branch, workspace_dir,
             json.dumps(transcript) if transcript is not None else None]
    if steps_used is not None:
        sets.append("steps_used=?")
        params.append(steps_used)
    if elapsed_seconds is not None:
        sets.append("elapsed_seconds=?")
        params.append(elapsed_seconds)
    params.append(row_id)
    conn.execute(f"UPDATE exec_queue SET {', '.join(sets)} WHERE id=?", params)


def exec_mark_failed(conn, row_id, error: str, max_attempts: int, backoff_base: int):
    """Retry with exponential backoff, or give up. Returns the new status."""
    row = conn.execute("SELECT attempts FROM exec_queue WHERE id=?", (row_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE exec_queue SET status='failed', attempts=?, last_error=?, "
            "completed_at=? WHERE id=?",
            (attempts, error[:2000], int(time.time()), row_id),
        )
        return "failed"
    delay = backoff_base * (2 ** (attempts - 1))
    conn.execute(
        "UPDATE exec_queue SET status='pending', attempts=?, last_error=?, "
        "next_attempt_at=?, claimed_at=NULL WHERE id=?",
        (attempts, error[:2000], int(time.time()) + delay, row_id),
    )
    return "pending"


def exec_chunk_summaries(conn, tududi_task_id, before_chunk_index):
    """Ordered list of each earlier chunk's finish() summary for this task
    (chunk_index < before_chunk_index, most-recently-completed row per
    index), for execution.build_prompt()'s PRIOR_CHUNKS_SUMMARY. Chunk rows
    are terminal ('done') by the time the next chunk is enqueued -- see
    exec_enqueue()'s docstring -- so this only ever looks at finished work."""
    rows = conn.execute(
        "SELECT chunk_index, result_json FROM exec_queue "
        "WHERE tududi_task_id=? AND status='done' AND chunk_index<? "
        "ORDER BY chunk_index",
        (tududi_task_id, before_chunk_index),
    ).fetchall()
    summaries = []
    for row in rows:
        result = json.loads(row["result_json"] or "{}")
        if result.get("summary"):
            summaries.append(result["summary"])
    return summaries


def exec_reclaim_stale(conn, stale_after: int) -> int:
    cutoff = int(time.time()) - stale_after
    cur = conn.execute(
        "UPDATE exec_queue SET status='pending', claimed_at=NULL "
        "WHERE status='processing' AND claimed_at < ?",
        (cutoff,),
    )
    return cur.rowcount


def exec_stats(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM exec_queue GROUP BY status"
    ).fetchall()
    return {r["status"]: r["c"] for r in rows}


def exec_get(conn, row_id):
    row = conn.execute("SELECT * FROM exec_queue WHERE id=?", (row_id,)).fetchone()
    return dict(row) if row else None


def exec_set_topic(conn, row_id, topic: str):
    conn.execute("UPDATE exec_queue SET ntfy_topic=? WHERE id=?", (topic, row_id))


def exec_set_workspace(conn, row_id, *, workspace_dir: str, branch: str):
    """Persisted as soon as the workspace clone + branch are created, not
    only at exec_mark_done() -- a row that later parks needs workspace_dir/
    branch to resume from, and park_awaiting() itself doesn't touch them."""
    conn.execute("UPDATE exec_queue SET workspace_dir=?, branch=? WHERE id=?",
                (workspace_dir, branch, row_id))


def exec_set_backend(conn, row_id, run_backend: str):
    """Resolved once at the start of a fresh run (see sandbox.py's module
    docstring on why this must stay fixed for the run's whole lifetime) and
    read back on every resume -- never recomputed mid-run."""
    conn.execute("UPDATE exec_queue SET run_backend=? WHERE id=?", (run_backend, row_id))


def exec_park_awaiting(conn, row_id, *, kind, question, pending_command, conversation_json,
                       elapsed_seconds, steps_used):
    """Pauses a row on a genuine question or a push/build approval -- `kind`
    is "question" or "approval". Unlike plan_park_awaiting, no
    correlation_token: the row's own ntfy_topic (set once, at discovery)
    already disambiguates any reply."""
    conn.execute(
        "UPDATE exec_queue SET status='awaiting_input', park_kind=?, question_text=?, "
        "pending_command=?, conversation_json=?, awaiting_since=?, elapsed_seconds=?, "
        "steps_used=?, claimed_at=NULL WHERE id=?",
        (kind, question, pending_command, conversation_json, int(time.time()),
         elapsed_seconds, steps_used, row_id),
    )


def exec_resume_with_reply(conn, row_id, reply_text: str):
    """Puts the row back in 'pending' so the executor's normal claim loop
    picks it up -- process() detects park_kind is still set and resumes
    instead of starting fresh. Deliberately leaves park_kind/question_text/
    pending_command/conversation_json in place (process() needs them to
    resume); only awaiting_since is cleared, since the row is no longer
    waiting."""
    conn.execute(
        "UPDATE exec_queue SET status='pending', reply_text=?, next_attempt_at=0, "
        "claimed_at=NULL, awaiting_since=NULL WHERE id=?",
        (reply_text, row_id),
    )


def exec_stale_awaiting(conn, reminder_after: int):
    """Rows unanswered longer than reminder_after seconds. 0 disables (returns [])."""
    if not reminder_after:
        return []
    cutoff = int(time.time()) - reminder_after
    rows = conn.execute(
        "SELECT * FROM exec_queue WHERE status='awaiting_input' AND awaiting_since < ?",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def exec_touch_awaiting(conn, row_id):
    conn.execute("UPDATE exec_queue SET awaiting_since=? WHERE id=?",
                 (int(time.time()), row_id))


def exec_awaiting_rows(conn):
    """All currently-parked rows -- used on executor startup to respawn a
    listener thread per row after a restart (in-memory listener threads
    don't survive one, but the parked DB state does). awaiting_since is the
    listener's ntfy cursor -- a genuine reply may have arrived while the
    executor was down, so the resumed listener must look back to park time,
    not just from restart time forward."""
    rows = conn.execute(
        "SELECT id, ntfy_topic, awaiting_since FROM exec_queue WHERE status='awaiting_input'"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- ollama lease ----------
# worker.py and executor.py both drive local Ollama inference, which must
# stay strictly serial (see worker.py's module docstring) -- this is a mutex
# built on top of the meta table (the only piece of shared, durable state
# both daemons' separate DB connections already agree on).

def acquire_ollama_lease(conn, holder: str, ttl: int) -> bool:
    """True if `holder` now owns the lease. An expired lease is treated as
    free so a crashed holder can't deadlock the queue forever -- callers
    should pass a ttl comfortably longer than the work they're about to do,
    and release explicitly (in a finally block) as soon as they're done."""
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        raw = get_meta(conn, "ollama_lease")
        if raw:
            lease = json.loads(raw)
            if lease.get("expiry", 0) > now:
                conn.execute("COMMIT")
                return False
        set_meta(conn, "ollama_lease", json.dumps({"holder": holder, "expiry": now + ttl}))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release_ollama_lease(conn, holder: str):
    """No-op if the lease isn't currently held by `holder` -- e.g. it already
    expired and was re-acquired by someone else; releasing it out from under
    them would defeat the mutex."""
    raw = get_meta(conn, "ollama_lease")
    if not raw:
        return
    lease = json.loads(raw)
    if lease.get("holder") == holder:
        conn.execute("DELETE FROM meta WHERE key='ollama_lease'")

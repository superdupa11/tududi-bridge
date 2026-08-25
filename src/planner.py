"""Planner: tag-triggered AI scoping for tasks already sitting in tududi.

    tududi (tag: plan-me) --poll--> planner --claude -p--> scoped plan --> tududi.note

Separate third daemon from ingest.py/worker.py, which stay untouched. Unlike
that pipeline (which captures new thoughts), this one reads existing tasks
back out of tududi, so it has its own queue table (plan_queue) with an
awaiting_input state the capture pipeline has no use for -- a task can sit
parked indefinitely while Claude waits on a clarifying question sent over
ntfy, without blocking any other queued task.
"""
import datetime as dt
import json
import logging
import os
import re
import secrets
import threading
import time

import config
import db
import ntfy
import planning
import repos
from claude_client import ClaudeClient, ClaudeError
from tududi import Tududi, TududiError

log = logging.getLogger("planner")

TAG_IN_PROGRESS = "plan:in-progress"
TAG_AWAITING = "plan:awaiting-input"
TAG_DONE = "plan:done"
TAG_FAILED = "plan:plan-failed"

REPLY_CURSOR_KEY = "ntfy_reply_cursor"
TOKEN_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L
TOKEN_RE = re.compile(r"PLAN-[A-Z0-9]{6}", re.IGNORECASE)


def assert_no_metered_billing_vars():
    """Refuse to start at all if ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is
    set anywhere in this process's environment. Claude Code's auth
    precedence prefers either over CLAUDE_CODE_OAUTH_TOKEN, so their mere
    presence would silently switch billing to pay-per-token -- this is the
    hard guarantee behind "no metered billing", not a doc note asking the
    operator to be careful. See claude_client.py's env allowlist for the
    complementary subprocess-level check.
    """
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(var):
            log.error(
                "%s is set in this container's environment. Refusing to start: "
                "Claude Code prefers it over CLAUDE_CODE_OAUTH_TOKEN and would "
                "silently switch to metered per-token billing. Remove %s from "
                "this container's environment -- it has no legitimate use here -- "
                "and restart.", var, var,
            )
            raise SystemExit(1)


def load_average():
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def in_quiet_hours(qh):
    if not qh:
        return True
    hour = dt.datetime.now().hour
    start, end = qh["start"], qh["end"]
    if start > end:                      # window wraps midnight, e.g. 23 -> 7
        return hour >= start or hour < end
    return start <= hour < end


def should_pause(cfg):
    if not in_quiet_hours(cfg.planner_quiet_hours):
        return "outside processing window"
    if cfg.planner_load_threshold and load_average() > cfg.planner_load_threshold:
        return f"load {load_average():.1f} > {cfg.planner_load_threshold}"
    return None


def owned_tags(cfg):
    return {cfg.planner_trigger_tag, TAG_IN_PROGRESS, TAG_AWAITING, TAG_DONE, TAG_FAILED}


def gen_token():
    return "PLAN-" + "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(6))


def discover(cfg, conn, td):
    try:
        tasks = td.list_tasks(tag=cfg.planner_trigger_tag)
    except TududiError as e:
        log.warning("discover failed: %s", e)
        return
    for t in tasks:
        task_id = t.get("uid") or t.get("uuid") or t.get("id")
        if task_id is None:
            continue
        row_id = db.plan_enqueue(
            conn, tududi_task_id=str(task_id),
            project_id=t.get("project_id"),
            task_name=t.get("name"),
            task_note=t.get("note") or "",
        )
        if row_id:
            log.info("discovered task %s -> plan_queue #%s", task_id, row_id)


def gather_repo(cfg, conversation, project_id):
    """Cached in `conversation` across clarification rounds so grounding
    doesn't shift mid-exchange."""
    if "repo_dir" in conversation:
        return conversation["repo_dir"], conversation.get("repo_label")
    owner_repo = cfg.github_repos.get(str(project_id))
    repo_dir = repos.ensure_repo_clone(cfg, project_id)
    conversation["repo_dir"] = str(repo_dir) if repo_dir else None
    conversation["repo_label"] = owner_repo if repo_dir else None
    return conversation["repo_dir"], conversation["repo_label"]


def start_clarification(cfg, td, conn, row, questions, conversation, current_tags, owned):
    token = gen_token()
    question_text = "; ".join(questions) if questions else "Need more info to plan this task."
    conversation["pending_question"] = question_text

    msg = (f"[{token}] Planning question for {row['task_name']!r}:\n\n{question_text}\n\n"
          f"Reply on this topic. Prefix with {token} if more than one question "
          f"is open at once (otherwise it's matched automatically).")
    ntfy.publish(cfg, cfg.ntfy_reply_topic, msg, title="tududi planner: question")

    task_id = row["tududi_task_id"]
    td.update_task(task_id, tags=planning.derive_plan_tags(current_tags, owned, TAG_AWAITING))

    db.plan_park_awaiting(conn, row["id"], correlation_token=token,
                          question_text=question_text,
                          conversation_json=json.dumps(conversation))
    log.info("#%s parked awaiting_input, token=%s", row["id"], token)


def finalize_plan(cfg, td, conn, row, plan, repo_label, conversation, meta,
                  current_tags, owned, task_note, effort):
    planned_at = time.strftime("%Y-%m-%d %H:%M")
    note = planning.render_plan(plan, task_note, repo_label, conversation,
                                cfg.claude_model, effort, meta.get("cost_usd"), planned_at)
    task_id = row["tududi_task_id"]
    tags = planning.derive_plan_tags(current_tags, owned, TAG_DONE)
    td.update_task(task_id, note=note, tags=tags)

    result = {"plan": plan, "usage": meta.get("usage"), "cost_usd": meta.get("cost_usd"),
              "rounds": len(conversation.get("rounds") or [])}
    db.plan_mark_done(conn, row["id"], result)
    log.info("#%s done -> task %s ($%.4f)", row["id"], task_id, meta.get("cost_usd") or 0)


def process(cfg, conn, td, claude, prompt, row):
    task_id = row["tududi_task_id"]
    task = td.get_task(task_id)
    task_name = task.get("name") or row["task_name"] or ""
    task_note = task.get("note") or row["task_note"] or ""
    current_tags = task.get("tags") or []
    project_id = row["project_id"]
    owned = owned_tags(cfg)

    td.update_task(task_id, tags=planning.derive_plan_tags(current_tags, owned, TAG_IN_PROGRESS))

    conversation = json.loads(row["conversation_json"] or "{}")
    repo_dir, repo_label = gather_repo(cfg, conversation, project_id)

    round_n = row["clarification_round"] or 0
    # Without a reply topic there is no way to ever resume a parked task, so
    # treat this the same as having exhausted the round budget -- Claude
    # always commits to a best-effort plan instead of asking a question that
    # can never be answered (start_clarification() requires a real topic).
    final_round = round_n >= cfg.max_clarification_rounds or not cfg.ntfy_reply_topic

    history_text = None
    if conversation.get("rounds"):
        history_text = "\n".join(
            f"Q{i}: {r['question']}\nA{i}: {r['answer']}"
            for i, r in enumerate(conversation["rounds"], 1)
        )

    prompt_text = planning.build_prompt(
        prompt, task_title=task_name, task_note=task_note,
        project_notes=cfg.notes_for_project(project_id),
        repo_dir=repo_dir, clarification_history=history_text,
        final_round=final_round,
    )

    plan, meta = claude.chat_json(prompt_text, prompt.schema, effort=prompt.effort,
                                  repo_dir=repo_dir)

    if plan.get("needs_clarification") and not final_round:
        start_clarification(cfg, td, conn, row, plan.get("questions") or [],
                            conversation, current_tags, owned)
        return

    if plan.get("needs_clarification") and final_round:
        conversation["unresolved_questions"] = plan.get("questions") or []

    finalize_plan(cfg, td, conn, row, plan, repo_label, conversation, meta,
                 current_tags, owned, task_note, prompt.effort)


def handle_reply(cfg, conn, msg):
    text = (msg.get("message") or "").strip()
    if not text:
        return

    row = None
    m = TOKEN_RE.search(text)
    if m:
        token = m.group(0).upper()
        row = db.plan_find_awaiting_by_token(conn, token)
        text = (text[:m.start()] + text[m.end():]).strip()
    if row is None:
        row = db.plan_find_sole_awaiting(conn)
    if row is None:
        log.warning("reply matched no awaiting_input task, dropping: %r", text[:100])
        return

    conversation = json.loads(row["conversation_json"] or "{}")
    question = conversation.get("pending_question") or row["question_text"] or ""
    conversation.setdefault("rounds", []).append({"question": question, "answer": text})
    conversation.pop("pending_question", None)

    db.plan_resume_with_reply(conn, row["id"], text, json.dumps(conversation))
    log.info("#%s resumed by ntfy reply", row["id"])


def listen_for_replies(cfg):
    """Runs on a background thread with its own DB connection -- sqlite3
    connections aren't safe to share across threads, the same reason
    ingest.py/worker.py are separate processes against the same WAL file."""
    conn = db.connect(config.DB_PATH)
    cursor = db.get_meta(conn, REPLY_CURSOR_KEY, "all")
    for msg in ntfy.subscribe_stream(cfg, cfg.ntfy_reply_topic, cursor):
        try:
            handle_reply(cfg, conn, msg)
        except Exception as e:
            log.error("failed handling reply %s: %s", msg.get("id"), e)
        db.set_meta(conn, REPLY_CURSOR_KEY, msg["id"])


def nudge_stale_awaiting(cfg, conn):
    for row in db.plan_stale_awaiting(conn, cfg.awaiting_input_reminder_after):
        try:
            ntfy.publish(cfg, cfg.ntfy_reply_topic,
                        f"[{row['correlation_token']}] Reminder — still waiting on: "
                        f"{row['question_text']}",
                        title="tududi planner: reminder")
            db.plan_touch_awaiting(conn, row["id"])
        except ntfy.NtfyError as e:
            log.warning("reminder publish failed for #%s: %s", row["id"], e)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    assert_no_metered_billing_vars()

    cfg = config.load()
    conn = db.connect(config.DB_PATH)
    td = Tududi(cfg)
    claude = ClaudeClient(cfg)
    prompt = planning.load_prompt(config.PROMPT_DIR / "10_plan.md")

    try:
        td.ping()
    except TududiError as e:
        log.error("cannot reach tududi: %s", e)
        raise SystemExit(1)

    if not cfg.claude_oauth_token:
        log.warning("CLAUDE_CODE_OAUTH_TOKEN is not set -- claude CLI calls will fail auth")

    if cfg.ntfy_reply_topic:
        threading.Thread(target=listen_for_replies, args=(cfg,), daemon=True).start()
    else:
        log.warning("ntfy.reply_topic not configured -- clarifying questions are disabled; "
                    "every task will get a best-effort plan instead of being asked about")

    paused_reason = None
    while True:
        n = db.plan_reclaim_stale(conn, cfg.planner_stale_after)
        if n:
            log.warning("reclaimed %s stale row(s)", n)

        reason = should_pause(cfg)
        if reason:
            if reason != paused_reason:
                log.info("paused: %s", reason)
                paused_reason = reason
            time.sleep(cfg.planner_poll_interval * 3)
            continue
        paused_reason = None

        discover(cfg, conn, td)
        if cfg.ntfy_reply_topic:
            nudge_stale_awaiting(cfg, conn)

        row = db.plan_claim_one(conn)
        if row is None:
            time.sleep(cfg.planner_poll_interval)
            continue

        try:
            process(cfg, conn, td, claude, prompt, row)
        except (ClaudeError, TududiError, ntfy.NtfyError, Exception) as e:
            status = db.plan_mark_failed(conn, row["id"], f"{type(e).__name__}: {e}",
                                         cfg.planner_max_attempts, cfg.planner_backoff_base)
            log.error("#%s -> %s: %s", row["id"], status, e)
            if status == "failed" and row["tududi_task_id"]:
                try:
                    task = td.get_task(row["tududi_task_id"])
                    tags = planning.derive_plan_tags(task.get("tags") or [],
                                                      owned_tags(cfg), TAG_FAILED)
                    td.update_task(row["tududi_task_id"], tags=tags)
                except TududiError:
                    pass


if __name__ == "__main__":
    main()

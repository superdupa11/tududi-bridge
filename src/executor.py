"""Executor: tag-triggered agentic execution of already-planned tududi tasks.

    tududi (tag: execute-me, plan:done) --poll--> executor --agent.py--> git branch + commit
                                                                       --> tududi.note

Fourth daemon, alongside ingest.py/worker.py/planner.py, which stay
untouched except for worker.py's Ollama-lease wrapping (see db.py's
acquire_ollama_lease). Reads the structured plan a completed planner.py run
already produced (db.plan_latest_result_for_task, falling back to
execution.plan_from_note when no plan_queue row survives) and hands it to a
bounded Ollama tool-calling agent (agent.py) that works inside a persistent
git workspace clone (repos.ensure_workspace_clone) via a pluggable command
sandbox (sandbox.py) -- `docker exec` into the code-server container by
default, so a human can watch the run happen live.

Each run gets its OWN freshly-generated ntfy topic (announced on the
project's own topic, or the triage/inbox topic if the task isn't mapped to a
project), rather than sharing planner.py's single static reply topic -- a
reply on that topic is unambiguously for this one run, no token-prefix
parsing needed. The agent can pause on that topic mid-run, without holding
the Ollama lease or blocking any other queued task, in two ways: calling
ask_question, or sandbox.py flagging a `git push`/`docker build`/`docker
push` as needing approval. Either pause parks the exec_queue row
(status='awaiting_input', mirroring plan_queue's identical state) with the
live agent conversation serialized to conversation_json, and a dedicated
listener thread picks up the first reply on that topic and flips the row
back to 'pending' with reply_text set -- process() then detects park_kind is
still set on a freshly-claimed row and resumes instead of starting over.

Deliberately does NOT call assert_no_metered_billing_vars() the way
planner.py does -- that guard exists because planner.py shells out to the
Claude Code CLI, which prefers ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN over
the subscription token if either is set. This daemon never shells out to
`claude` at all, so the guard has nothing to protect here.
"""
import datetime as dt
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

import agent
import config
import db
import execution
import ntfy
import planner
import repos
import sandbox
from ollama import Ollama, OllamaError
from repos import RepoError
from tududi import Tududi, TududiError

log = logging.getLogger("executor")

TAG_IN_PROGRESS = "exec:in-progress"
TAG_AWAITING_INPUT = "exec:awaiting-input"
TAG_DONE = "exec:done"
TAG_NEEDS_REVIEW = "exec:needs-review"
TAG_FAILED = "exec:failed"

_APPROVE_WORDS = {"y", "yes", "approve", "approved", "ok", "okay", "go", "lgtm", "yep", "sure"}


class ExecutorError(RuntimeError):
    pass


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
    if not in_quiet_hours(cfg.executor_quiet_hours):
        return "outside processing window"
    if cfg.executor_load_threshold and load_average() > cfg.executor_load_threshold:
        return f"load {load_average():.1f} > {cfg.executor_load_threshold}"
    return None


def owned_tags(cfg):
    return {cfg.executor_trigger_tag, TAG_IN_PROGRESS, TAG_AWAITING_INPUT, TAG_DONE,
           TAG_NEEDS_REVIEW, TAG_FAILED}


def gen_token():
    """Reuses planner's no-ambiguous-character alphabet for the branch-name
    suffix -- no reason to invent a second one."""
    return "".join(secrets.choice(planner.TOKEN_ALPHABET) for _ in range(6)).lower()


def _new_topic(task_id):
    """High-entropy per-run topic name -- same 'topic name is a password'
    posture as every other ntfy topic in this project (see config.yml)."""
    return f"exec-{task_id}-{secrets.token_hex(8)}"


def _parse_approval(text: str) -> bool:
    words = (text or "").strip().lower().split()
    return bool(words) and words[0] in _APPROVE_WORDS


def _make_ntfy_publish(cfg, topic):
    def _publish(message, attach_path):
        if attach_path:
            ntfy.publish_file(cfg, topic, attach_path, message=message,
                              title="tududi executor: update")
        else:
            ntfy.publish(cfg, topic, message or "(no message)", title="tududi executor: update")
    return _publish


def _run_git(cwd, args, *, capture=False, check=True, timeout=60):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise ExecutorError(f"git {' '.join(args)} failed: {r.stderr[-400:]}")
    return r.stdout if capture else None


def _base_branch(workspace_dir):
    r = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                       cwd=str(workspace_dir), capture_output=True, text=True, timeout=10)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def discover(cfg, conn, td):
    try:
        tasks = td.list_tasks(tag=cfg.executor_trigger_tag)
    except TududiError as e:
        log.warning("discover failed: %s", e)
        return
    for t in tasks:
        task_id = t.get("uid") or t.get("uuid") or t.get("id")
        if task_id is None:
            continue
        task_id = str(task_id)
        plan = db.plan_latest_result_for_task(conn, task_id) or \
              execution.plan_from_note(t.get("note") or "") or {}
        row_id = db.exec_enqueue(
            conn, tududi_task_id=task_id,
            project_id=t.get("project_id"),
            task_name=t.get("name"),
            plan_json=json.dumps(plan),
        )
        if row_id:
            log.info("discovered task %s -> exec_queue #%s", task_id, row_id)


def _skip_needs_plan(cfg, td, conn, row, task_id, task_note, current_tags, owned):
    note = (task_note + "\n\n" if task_note else "") + (
        "## Execution\n_Skipped: this task doesn't carry the "
        f"`{planner.TAG_DONE}` tag yet, so there's no completed plan to execute. "
        f"Run the planner first, then re-add `{cfg.executor_trigger_tag}`._"
    )
    tags = execution.derive_exec_tags(current_tags, owned, TAG_NEEDS_REVIEW)
    td.update_task(task_id, note=note, tags=tags)
    db.exec_mark_done(conn, row["id"], {"skipped": f"missing {planner.TAG_DONE} tag"})
    log.info("#%s skipped (no %s tag) -> task %s", row["id"], planner.TAG_DONE, task_id)


def _skip_backend_unavailable(td, conn, row, task_id, task_note, current_tags, owned, detail):
    note = task_note + (
        f"\n\n## Execution\n_Skipped: the sandbox backend isn't reachable ({detail}). "
        "Fix the code-server/docker.sock setup, then re-add the trigger tag to retry._"
    )
    tags = execution.derive_exec_tags(current_tags, owned, TAG_NEEDS_REVIEW)
    td.update_task(task_id, note=note, tags=tags)
    db.exec_mark_done(conn, row["id"], {"skipped": f"sandbox backend unavailable: {detail}"})
    log.warning("#%s skipped (sandbox backend unavailable) -> task %s: %s",
               row["id"], task_id, detail)


def _skip_no_changes(td, conn, row, task_id, task_note, current_tags, owned, branch,
                     backend_note=None, transcript=None, report=None):
    extra = f" {backend_note}" if backend_note else ""
    note = task_note + (
        f"\n\n## Execution\n_The agent ran but produced no changes on branch `{branch}`.{extra}_"
    )
    tags = execution.derive_exec_tags(current_tags, owned, TAG_NEEDS_REVIEW)
    td.update_task(task_id, note=note, tags=tags)
    result = {"branch": branch, "no_changes": True}
    if report is not None:
        result["agent_report"] = report
    db.exec_mark_done(conn, row["id"], result, branch=branch, transcript=transcript)
    log.info("#%s -> task %s: no changes produced on %s", row["id"], task_id, branch)


MAX_TRANSCRIPT_EXCERPT = 6000


def _transcript_excerpt(transcript):
    return json.dumps(transcript, indent=2)[-MAX_TRANSCRIPT_EXCERPT:]


# ---------- parked-row listener threads ----------
# Ephemeral (in-memory only) -- db.exec_awaiting_rows() is how a restarted
# executor rediscovers which rows still need one, since the parked DB state
# itself does survive a restart.

def _listener_thread(cfg, row_id, topic):
    conn = db.connect(config.DB_PATH)
    try:
        for msg in ntfy.subscribe_stream(cfg, topic, "all"):
            text = (msg.get("message") or "").strip()
            if not text:
                continue
            db.exec_resume_with_reply(conn, row_id, text)
            log.info("#%s resumed by reply on %s", row_id, topic)
            return
    except Exception as e:  # noqa: BLE001 -- a dead listener thread must not die silently
        log.error("listener thread for #%s (%s) crashed: %s", row_id, topic, e)


def _spawn_listener(cfg, row_id, topic):
    threading.Thread(target=_listener_thread, args=(cfg, row_id, topic), daemon=True).start()


def _resume_listeners_on_startup(cfg, conn):
    for row in db.exec_awaiting_rows(conn):
        if row["ntfy_topic"]:
            _spawn_listener(cfg, row["id"], row["ntfy_topic"])
            log.info("resumed listener for parked #%s on %s", row["id"], row["ntfy_topic"])


def _nudge_stale_awaiting(cfg, conn):
    for row in db.exec_stale_awaiting(conn, cfg.executor_awaiting_input_reminder_after):
        topic = row["ntfy_topic"]
        if not topic:
            continue
        try:
            if row["park_kind"] == "approval":
                msg = (f"Reminder -- approval still pending:\n`{row['pending_command']}`\n"
                      f"{row['question_text']}")
            else:
                msg = f"Reminder -- still waiting on: {row['question_text']}"
            ntfy.publish(cfg, topic, msg, title="tududi executor: reminder")
            db.exec_touch_awaiting(conn, row["id"])
        except ntfy.NtfyError as e:
            log.warning("reminder publish failed for #%s: %s", row["id"], e)


# ---------- automatic end-of-run push approval ----------
# Separate, lighter-weight than the exec_queue park/resume machinery above --
# by this point the agent loop is fully finished and the row is already
# marked done with its final result; approving or denying the push doesn't
# change that outcome, so it doesn't need to be resumable DB state. A
# restart before the human replies loses the prompt (not the code -- it's
# already committed locally on the branch, reviewable in code-server, and
# push-able by hand).

def _maybe_request_push_approval(cfg, row_id, workspace_dir, branch, topic):
    if not (cfg.executor_allow_push and cfg.github_token) or not topic:
        return
    try:
        ntfy.publish(cfg, topic,
                    f"Approval needed: push branch `{branch}` to origin?\n\n"
                    "Reply 'yes' to push, anything else to skip (the branch stays local).",
                    title="tududi executor: push approval")
    except ntfy.NtfyError as e:
        log.warning("#%s could not request push approval: %s", row_id, e)
        return
    threading.Thread(target=_push_approval_listener,
                     args=(cfg, row_id, topic, str(workspace_dir), branch), daemon=True).start()


def _push_approval_listener(cfg, row_id, topic, workspace_dir, branch):
    try:
        for msg in ntfy.subscribe_stream(cfg, topic, "all"):
            text = (msg.get("message") or "").strip()
            if not text:
                continue
            if _parse_approval(text):
                r = subprocess.run(["git", "push", "-u", "origin", branch], cwd=workspace_dir,
                                   capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    log.info("#%s pushed branch %s (approved via ntfy)", row_id, branch)
                    msg_out = f"Pushed `{branch}` to origin."
                else:
                    log.warning("#%s push failed after approval: %s", row_id, r.stderr[-300:])
                    msg_out = f"Push failed: {r.stderr[-300:]}"
            else:
                log.info("#%s push denied via ntfy reply", row_id)
                msg_out = "OK, not pushing. The branch is still available locally."
            try:
                ntfy.publish(cfg, topic, msg_out, title="tududi executor")
            except ntfy.NtfyError:
                pass
            return
    except Exception as e:  # noqa: BLE001
        log.error("push-approval listener for #%s (%s) crashed: %s", row_id, topic, e)


def _backend_note(cfg, project_id, run_backend):
    """Non-None only for a Mac-routed project that ended up NOT on the Mac
    backend -- i.e. it was reachable-checked and found unreachable at run
    start (see mac_reachable()). Recomputed from (project_id, run_backend)
    rather than stored as text, so it stays correct across any number of
    park/resume cycles without needing its own persisted copy."""
    if str(project_id) in cfg.mac_projects and run_backend != "mac":
        return (f"This project is Mac-routed (mac.projects), but the Mac backend was not "
               f"reachable when this run started -- fell back to the {run_backend!r} backend. "
               "Anything needing real Apple tooling (Xcode/iOS Simulator) could not be "
               "verified here.")
    return None


def process(cfg, conn, td, llm, prompt, row):
    task_id = row["tududi_task_id"]
    task = td.get_task(task_id)
    task_name = task.get("name") or row["task_name"] or ""
    task_note = task.get("note") or ""
    current_tags = task.get("tags") or []
    project_id = row["project_id"]
    owned = owned_tags(cfg)

    resuming = bool(row["park_kind"])
    prompt_text = None

    if resuming:
        topic = row["ntfy_topic"]
        workspace_dir = Path(row["workspace_dir"])
        branch = row["branch"]
        run_backend = row["run_backend"] or cfg.exec_backend
        conversation = json.loads(row["conversation_json"] or "[]")
        elapsed_so_far = row["elapsed_seconds"] or 0.0
        steps_so_far = row["steps_used"] or 0
        reply = row["reply_text"] or ""

        if row["park_kind"] == "approval":
            pending_command = row["pending_command"] or ""
            if _parse_approval(reply):
                code, out, err = sandbox.run(cfg, pending_command, cwd=str(workspace_dir),
                                             timeout=cfg.executor_step_timeout, pre_approved=True,
                                             backend=run_backend)
                result_text = f"[approved by human]\nexit={code}\nstdout:\n{out}\nstderr:\n{err}"
            else:
                result_text = f"[denied by human]: {reply or 'no reason given'}"
            conversation.append({"role": "tool", "name": "run", "content": result_text})
        else:  # "question"
            conversation.append({"role": "tool", "name": "ask_question", "content": reply})

        td.update_task(task_id, tags=execution.derive_exec_tags(current_tags, owned, TAG_IN_PROGRESS))
        try:
            ntfy.publish(cfg, topic, "Got it, continuing...", title="tududi executor")
        except ntfy.NtfyError as e:
            log.warning("#%s ack publish failed (continuing anyway): %s", row["id"], e)

    else:
        if planner.TAG_DONE not in current_tags:
            _skip_needs_plan(cfg, td, conn, row, task_id, task_note, current_tags, owned)
            return

        backend_ok, backend_detail = sandbox.preflight(cfg)
        if not backend_ok:
            _skip_backend_unavailable(td, conn, row, task_id, task_note, current_tags, owned,
                                      backend_detail)
            return

        td.update_task(task_id, tags=execution.derive_exec_tags(current_tags, owned, TAG_IN_PROGRESS))

        plan = json.loads(row["plan_json"] or "{}")
        if not plan:
            plan = db.plan_latest_result_for_task(conn, task_id) or \
                  execution.plan_from_note(task_note) or {}

        workspace_dir = repos.ensure_workspace_clone(cfg, project_id)

        token = gen_token()
        branch = f"bridge/exec-{task_id}-{token}"
        _run_git(workspace_dir, ["checkout", _base_branch(workspace_dir)])
        _run_git(workspace_dir, ["pull", "--ff-only"], check=False)
        _run_git(workspace_dir, ["checkout", "-b", branch])

        db.exec_set_workspace(conn, row["id"], workspace_dir=str(workspace_dir), branch=branch)

        # Resolved once, here, and persisted -- NOT re-checked on every
        # resume, so a Mac that drops off the network mid-run doesn't flip
        # the backend under the agent partway through. See sandbox.py.
        if str(project_id) in cfg.mac_projects and sandbox.mac_reachable(cfg):
            run_backend = "mac"
        else:
            run_backend = cfg.exec_backend
        db.exec_set_backend(conn, row["id"], run_backend)

        topic = _new_topic(task_id)
        db.exec_set_topic(conn, row["id"], topic)

        announce_topic = cfg.topic_for_project(project_id)
        if announce_topic:
            try:
                ntfy.publish(cfg, announce_topic,
                            f"Executing '{task_name}' (task {task_id}) on branch `{branch}`.\n"
                            f"Follow along or reply on this run's own topic: {topic}",
                            title="tududi executor: started")
            except ntfy.NtfyError as e:
                log.warning("#%s announce publish failed (continuing anyway): %s", row["id"], e)
            # Separate, minimal message with nothing but the bare topic name --
            # the sentence above reads better, but isn't clean to copy-paste
            # straight into ntfy's "subscribe to topic" field; this is.
            try:
                ntfy.publish(cfg, announce_topic, topic, title="tududi executor: topic")
            except ntfy.NtfyError as e:
                log.warning("#%s topic-name publish failed (continuing anyway): %s", row["id"], e)

        opening_msg = (f"Started executing '{task_name}' on branch `{branch}`. I'll post "
                      "questions, approvals, and updates here.")
        backend_note = _backend_note(cfg, project_id, run_backend)
        if backend_note:
            opening_msg += f"\n\n{backend_note}"
        try:
            ntfy.publish(cfg, topic, opening_msg, title="tududi executor: started")
        except ntfy.NtfyError as e:
            log.warning("#%s topic-open publish failed (continuing anyway): %s", row["id"], e)

        prompt_text = execution.build_prompt(prompt, task_title=task_name, plan=plan,
                                             workspace_dir=workspace_dir)
        conversation = None
        elapsed_so_far = 0.0
        steps_so_far = 0

    holder = f"executor:{os.getpid()}"
    lease_ttl = max(cfg.executor_total_timeout - elapsed_so_far, 60) + 120
    while not db.acquire_ollama_lease(conn, holder, ttl=lease_ttl):
        log.info("waiting for ollama lease")
        time.sleep(3)
    try:
        outcome, transcript = agent.run(
            cfg, llm, prompt.system, prompt_text, str(workspace_dir), _make_ntfy_publish(cfg, topic),
            max_steps=prompt.max_steps, resume_messages=conversation,
            elapsed_so_far=elapsed_so_far, steps_so_far=steps_so_far, backend=run_backend,
        )
    finally:
        db.release_ollama_lease(conn, holder)

    if outcome["status"] == "parked":
        db.exec_park_awaiting(
            conn, row["id"], kind=outcome["kind"], question=outcome["question"],
            pending_command=outcome.get("pending_command"),
            conversation_json=json.dumps(outcome["resume_messages"]),
            elapsed_seconds=outcome["elapsed_seconds"], steps_used=outcome["steps_used"],
        )
        fresh_tags = td.get_task(task_id).get("tags") or []
        td.update_task(task_id, tags=execution.derive_exec_tags(fresh_tags, owned,
                                                                 TAG_AWAITING_INPUT))
        if outcome["kind"] == "approval":
            park_msg = (f"Approval needed:\n`{outcome['pending_command']}`\n\n"
                       f"{outcome['question']}\n\nReply 'yes' to approve, anything else to deny.")
        else:
            park_msg = f"Question: {outcome['question']}\n\nReply on this topic with your answer."
        try:
            ntfy.publish(cfg, topic, park_msg, title="tududi executor: input needed")
        except ntfy.NtfyError as e:
            log.warning("#%s park notification failed: %s", row["id"], e)
        _spawn_listener(cfg, row["id"], topic)
        log.info("#%s parked (%s) -> topic %s", row["id"], outcome["kind"], topic)
        return

    report = outcome["report"]
    steps_used = outcome["steps_used"]
    seconds = outcome["elapsed_seconds"]

    porcelain = _run_git(workspace_dir, ["status", "--porcelain"], capture=True)
    if not (porcelain or "").strip():
        _skip_no_changes(td, conn, row, task_id, task_note, current_tags, owned, branch,
                         backend_note=_backend_note(cfg, project_id, run_backend),
                         transcript=transcript, report=report)
        return

    _run_git(workspace_dir, ["add", "-A"])
    _run_git(workspace_dir, ["commit", "-m", f"{task_name}\n\ntududi-task: {task_id}"])

    base_branch = _base_branch(workspace_dir)
    diffstat = _run_git(workspace_dir, ["diff", "--stat", f"{base_branch}...{branch}"],
                        capture=True, check=False) or ""

    executed_at = time.strftime("%Y-%m-%d %H:%M")
    report_section = execution.render_report(
        report, branch=branch, diffstat=diffstat, model=cfg.exec_model, steps_used=steps_used,
        seconds=seconds, transcript_excerpt=_transcript_excerpt(transcript), executed_at=executed_at,
        backend_note=_backend_note(cfg, project_id, run_backend),
    )
    note = task_note + "\n\n" + report_section

    checks = report.get("acceptance_check") or []
    all_met = bool(checks) and all(
        isinstance(c, dict) and c.get("status") == "met" for c in checks
    )
    new_status = TAG_DONE if all_met else TAG_NEEDS_REVIEW
    tags = execution.derive_exec_tags(current_tags, owned, new_status)
    td.update_task(task_id, note=note, tags=tags)

    result = {"branch": branch, "acceptance_check": checks, "confidence": report.get("confidence"),
             "steps_used": steps_used, "seconds": round(seconds, 1)}
    db.exec_mark_done(conn, row["id"], result, branch=branch, workspace_dir=str(workspace_dir),
                      transcript=transcript, steps_used=steps_used, elapsed_seconds=seconds)

    log.info("#%s done -> task %s (%s, %s step(s), %.0fs)", row["id"], task_id, new_status,
            steps_used, seconds)
    if cfg.executor_notify_topic:
        try:
            ntfy.publish(cfg, cfg.executor_notify_topic,
                        f"{task_name}: {new_status} on branch {branch} ({steps_used} steps, "
                        f"{seconds:.0f}s)", title="tududi executor")
        except ntfy.NtfyError as e:
            log.warning("notify failed for #%s: %s", row["id"], e)

    _maybe_request_push_approval(cfg, row["id"], workspace_dir, branch, topic)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = config.load()
    conn = db.connect(config.DB_PATH)
    td = Tududi(cfg)
    prompt = execution.load_prompt(config.PROMPT_DIR / "20_execute.md")
    # The prompt file's model/num_ctx are optional overrides on top of
    # cfg.exec_model/cfg.exec_num_ctx -- see execution.py's ExecPrompt.
    llm = Ollama(cfg, model=prompt.model or cfg.exec_model,
                num_ctx=prompt.num_ctx or cfg.exec_num_ctx)

    try:
        td.ping()
    except TududiError as e:
        log.error("cannot reach tududi: %s", e)
        raise SystemExit(1)

    ok, detail = sandbox.preflight(cfg)
    if not ok:
        log.warning("sandbox preflight failed (%s) -- tasks will be retagged %s until fixed: %s",
                   cfg.exec_backend, TAG_NEEDS_REVIEW, detail)
    else:
        log.info("sandbox preflight OK: %s", detail)

    _resume_listeners_on_startup(cfg, conn)

    paused_reason = None
    while True:
        n = db.exec_reclaim_stale(conn, cfg.executor_stale_after)
        if n:
            log.warning("reclaimed %s stale row(s)", n)

        reason = should_pause(cfg)
        if reason:
            if reason != paused_reason:
                log.info("paused: %s", reason)
                paused_reason = reason
            time.sleep(cfg.executor_poll_interval * 3)
            continue
        paused_reason = None

        discover(cfg, conn, td)
        _nudge_stale_awaiting(cfg, conn)

        row = db.exec_claim_one(conn)
        if row is None:
            time.sleep(cfg.executor_poll_interval)
            continue

        try:
            process(cfg, conn, td, llm, prompt, row)
        except (ExecutorError, RepoError, OllamaError, TududiError, ntfy.NtfyError, Exception) as e:
            status = db.exec_mark_failed(conn, row["id"], f"{type(e).__name__}: {e}",
                                         cfg.executor_max_attempts, cfg.executor_backoff_base)
            log.error("#%s -> %s: %s", row["id"], status, e)
            if status == "failed" and row["tududi_task_id"]:
                try:
                    task = td.get_task(row["tududi_task_id"])
                    tags = execution.derive_exec_tags(task.get("tags") or [],
                                                       owned_tags(cfg), TAG_FAILED)
                    td.update_task(row["tududi_task_id"], tags=tags)
                except TududiError:
                    pass
            if status == "failed" and cfg.executor_notify_topic:
                try:
                    ntfy.publish(cfg, cfg.executor_notify_topic,
                                f"execution failed for task {row['tududi_task_id']}: {e}",
                                title="tududi executor: failed")
                except ntfy.NtfyError:
                    pass


if __name__ == "__main__":
    main()

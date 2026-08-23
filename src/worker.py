"""Worker: drain the queue, one item at a time, through the 3-pass pipeline.

Strictly serial. Two concurrent CPU inferences on a dual-channel box are slower
in aggregate than one, because both are competing for the same memory bandwidth.
"""
import datetime as dt
import json
import logging
import os
import time

import config
import db
import pipeline
from ollama import Ollama, OllamaError
from tududi import Tududi, TududiError

log = logging.getLogger("worker")


def load_average():
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def in_quiet_hours(qh):
    """Optional window during which the worker is allowed to run at all."""
    if not qh:
        return True
    hour = dt.datetime.now().hour
    start, end = qh["start"], qh["end"]
    if start > end:                      # window wraps midnight, e.g. 23 -> 7
        return hour >= start or hour < end
    return start <= hour < end


def should_pause(cfg):
    if not in_quiet_hours(cfg.quiet_hours):
        return "outside processing window"
    if cfg.load_threshold and load_average() > cfg.load_threshold:
        return f"load {load_average():.1f} > {cfg.load_threshold}"
    return None


def process(cfg, conn, td, llm, prompts, row):
    raw = row["raw_text"]
    topic = row["topic"]
    project_id, notes = cfg.project_for(topic)
    hint_tags = json.loads(row["hint_tags"] or "[]")
    captured = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["received_at"]))

    log.info("#%s [%s] %r", row["id"], topic, raw[:70])
    t0 = time.time()

    final, telemetry = pipeline.run(
        llm, prompts,
        raw_text=raw,
        project_name=topic,
        project_notes=notes,
        hint_tags=hint_tags,
        hint_priority=row["hint_priority"],
        log=lambda m: log.info(m),
    )

    title = (final.get("title") or raw.split("\n")[0])[:255]
    description = pipeline.render_description(final, raw, topic, captured)
    tags = pipeline.derive_tags(final)

    task_id = row["tududi_task_id"]
    if task_id:
        td.update_task(task_id, name=title, description=description, tags=tags)
    else:
        # Stub creation failed at ingest time; create it now.
        task_id = td.create_task(name=title, project_id=project_id,
                                 description=description, tags=tags)
        db.attach_task(conn, row["id"], task_id)

    telemetry["seconds"] = round(time.time() - t0, 1)
    telemetry["task_id"] = task_id
    db.mark_done(conn, row["id"], telemetry)
    log.info("#%s done in %ss -> task %s", row["id"], telemetry["seconds"], task_id)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = config.load()
    conn = db.connect(config.DB_PATH)
    td = Tududi(cfg)
    llm = Ollama(cfg)
    prompts = pipeline.load_all(config.PROMPT_DIR)

    log.info("model=%s threads=%s ctx=%s", cfg.model, cfg.num_thread, cfg.num_ctx)
    try:
        llm.warm()
        log.info("model warmed")
    except Exception as e:
        log.warning("warm-up failed (continuing): %s", e)

    paused_reason = None
    while True:
        n = db.reclaim_stale(conn, cfg.stale_after)
        if n:
            log.warning("reclaimed %s stale row(s)", n)

        reason = should_pause(cfg)
        if reason:
            if reason != paused_reason:
                log.info("paused: %s", reason)
                paused_reason = reason
            time.sleep(cfg.poll_interval * 3)
            continue
        paused_reason = None

        row = db.claim_one(conn)
        if row is None:
            time.sleep(cfg.poll_interval)
            continue

        try:
            process(cfg, conn, td, llm, prompts, row)
        except (OllamaError, TududiError, Exception) as e:
            status = db.mark_failed(conn, row["id"], f"{type(e).__name__}: {e}",
                                    cfg.max_attempts, cfg.backoff_base)
            log.error("#%s -> %s: %s", row["id"], status, e)
            if status == "failed" and row["tududi_task_id"]:
                try:
                    td.update_task(row["tududi_task_id"],
                                   tags=["triage:failed", "needs-refinement"])
                except TududiError:
                    pass


if __name__ == "__main__":
    main()

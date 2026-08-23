"""Ingest: subscribe to ntfy topics, persist, create a stub task immediately.

Capture must never block on inference. This process does two things per message
and both are fast:
  1. write a durable queue row (unique on ntfy message id, so replays are safe)
  2. create a placeholder task in tududi so the thought is visible in seconds

The worker later enriches that same task in place.
"""
import json
import logging
import sys
import time

import requests

import config
import db
from tududi import Tududi, TududiError

log = logging.getLogger("ingest")
CURSOR_KEY = "ntfy_cursor"


def stub_description(raw, topic, ts):
    return (f"_Captured via ntfy `{topic}` at {ts}. "
            f"Awaiting automatic triage._\n\n---\n## Original capture\n> "
            + raw.replace("\n", "\n> "))


def handle(cfg, conn, td, msg):
    topic = msg.get("topic", "")
    raw = (msg.get("message") or "").strip()
    if not raw:
        return

    project_id, _notes = cfg.project_for(topic)
    tags = msg.get("tags") or []
    priority = msg.get("priority")

    row_id = db.enqueue(
        conn,
        ntfy_msg_id=msg["id"],
        topic=topic,
        raw_text=raw,
        hint_title=msg.get("title"),
        hint_tags=tags,
        hint_priority=priority,
        project_id=project_id,
    )
    if row_id is None:
        log.debug("duplicate %s, skipping", msg["id"])
        return

    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.get("time", time.time())))
    title = (msg.get("title") or raw.split("\n")[0])[:120]

    try:
        task_id = td.create_task(
            name=title,
            project_id=project_id,
            description=stub_description(raw, topic, ts),
            tags=["triage:pending"],
        )
        db.attach_task(conn, row_id, task_id)
        log.info("queued #%s -> task %s [%s] %r", row_id, task_id, topic, title[:60])
    except TududiError as e:
        # Row is already durable; the worker will create the task if it's missing.
        log.error("stub creation failed for #%s (will retry in worker): %s", row_id, e)


def stream(cfg, conn, td):
    topics = ",".join(cfg.topic_list)
    if not topics:
        log.error("no topics configured")
        sys.exit(1)

    headers = {}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"

    backoff = 1
    while True:
        cursor = db.get_meta(conn, CURSOR_KEY, "all")
        url = f"{cfg.ntfy_base}/{topics}/json"
        log.info("subscribing to %s (since=%s)", topics, cursor)
        try:
            with requests.get(url, params={"since": cursor}, headers=headers,
                              stream=True, timeout=(10, 300)) as r:
                r.raise_for_status()
                backoff = 1
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("event") != "message":
                        continue
                    handle(cfg, conn, td, msg)
                    db.set_meta(conn, CURSOR_KEY, msg["id"])
        except Exception as e:
            log.warning("stream dropped (%s), reconnecting in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = config.load()
    conn = db.connect(config.DB_PATH)
    td = Tududi(cfg)
    try:
        td.ping()
    except TududiError as e:
        log.error("cannot reach tududi: %s", e)
        sys.exit(1)
    stream(cfg, conn, td)


if __name__ == "__main__":
    main()

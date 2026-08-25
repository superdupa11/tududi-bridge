"""ntfy outbound publish + a reusable single-topic subscribe generator.

ingest.py's stream() remains the only reader for capture topics and is not
touched here -- it's tightly coupled to queue-row/stub-task creation. This
module exists for the planner's separate need: publishing clarifying
questions, and listening for replies on one static reply topic.
"""
import json
import logging
import time

import requests

log = logging.getLogger("ntfy")


class NtfyError(RuntimeError):
    pass


def publish(cfg, topic, message, *, title=None, tags=None, priority=None):
    headers = {}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = ",".join(tags) if isinstance(tags, (list, tuple)) else tags
    if priority:
        headers["Priority"] = str(priority)
    try:
        r = requests.post(f"{cfg.ntfy_base}/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        raise NtfyError(f"publish to {topic}: {e}") from e


def subscribe_stream(cfg, topic, cursor):
    """Blocking generator, same long-poll + backoff mechanics as
    ingest.py's stream() but scoped to one topic. Yields each ntfy message
    dict as it arrives (already filtered to event=="message"). Tracks its
    own reconnect cursor internally starting from the initial `cursor`
    argument, but does not persist it -- the caller owns persistence (via
    db.get_meta/set_meta) and should pass the last-seen cursor back in after
    a process restart.
    """
    headers = {}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"

    backoff = 1
    while True:
        url = f"{cfg.ntfy_base}/{topic}/json"
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
                    cursor = msg["id"]
                    yield msg
        except Exception as e:
            log.warning("reply stream dropped (%s), reconnecting in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

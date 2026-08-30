"""ntfy outbound publish + a reusable single-topic subscribe generator.

ingest.py's stream() remains the only reader for capture topics and is not
touched here -- it's tightly coupled to queue-row/stub-task creation. This
module exists for the planner's separate need: publishing clarifying
questions, and listening for replies on one static reply topic.
"""
import json
import logging
import os
import time

import requests

log = logging.getLogger("ntfy")

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # conservative; ntfy.sh's own default cap is 15MB


class NtfyError(RuntimeError):
    pass


def publish_file(cfg, topic, file_path, *, message=None, title=None):
    """Publishes a local file as an ntfy attachment (agent.py's send_update
    tool) -- e.g. a screenshot or log the agent already produced on disk.
    `message` becomes the accompanying caption, not the attachment content."""
    size = os.path.getsize(file_path)
    if size > MAX_ATTACHMENT_BYTES:
        raise NtfyError(f"{file_path} is {size} bytes, over the {MAX_ATTACHMENT_BYTES}-byte "
                        "attachment limit -- send a summary as text instead")
    headers = {"Filename": os.path.basename(file_path)}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    if message:
        headers["Message"] = message
    if title:
        headers["Title"] = title
    with open(file_path, "rb") as fh:
        data = fh.read()
    try:
        r = requests.put(f"{cfg.ntfy_base}/{topic}", data=data, headers=headers, timeout=60)
        r.raise_for_status()
    except requests.RequestException as e:
        raise NtfyError(f"publish_file to {topic}: {e}") from e


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


def subscribe_stream(cfg, topic, cursor, *, stop_event=None, timeout=(10, 300)):
    """Blocking generator, same long-poll + backoff mechanics as
    ingest.py's stream() but scoped to one topic. Yields each ntfy message
    dict as it arrives (already filtered to event=="message"). Tracks its
    own reconnect cursor internally starting from the initial `cursor`
    argument, but does not persist it -- the caller owns persistence (via
    db.get_meta/set_meta) and should pass the last-seen cursor back in after
    a process restart.

    `stop_event`, when given, is checked at the top of each reconnect cycle
    (and between yielded lines) -- setting it lets a caller tear this
    generator down promptly instead of leaving it blocked forever on a
    topic nobody's listening for replies on anymore. `timeout` overrides
    the default (10s connect, 300s read) -- executor.py's stop-listener
    uses a much shorter read timeout so it notices `stop_event` quickly.
    """
    headers = {}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"

    backoff = 1
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        url = f"{cfg.ntfy_base}/{topic}/json"
        try:
            with requests.get(url, params={"since": cursor}, headers=headers,
                              stream=True, timeout=timeout) as r:
                r.raise_for_status()
                backoff = 1
                for line in r.iter_lines(decode_unicode=True):
                    if stop_event is not None and stop_event.is_set():
                        return
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
        except requests.exceptions.Timeout:
            # Expected, routine event for a short-timeout caller (e.g.
            # executor.py's stop-listener, timeout=(10, 20)) -- the topic
            # was simply quiet longer than this listener's own read
            # timeout, not a real connection problem, so it doesn't warrant
            # a WARNING or the backoff a genuine failure gets. Reconnecting
            # immediately (backoff reset, not grown) is also what keeps a
            # short-timeout listener actually responsive -- growing the
            # backoff on every idle timeout would silently make it slower
            # over time, defeating the point of a short timeout at all.
            backoff = 1
        except Exception as e:
            log.warning("reply stream dropped (%s), reconnecting in %ss", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

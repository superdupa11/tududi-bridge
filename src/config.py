"""Configuration loading. Env vars override the YAML file for secrets."""
import os
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", "/config/config.yml"))
PROMPT_DIR = Path(os.environ.get("BRIDGE_PROMPTS", "/app/prompts"))
DB_PATH = Path(os.environ.get("BRIDGE_DB", "/data/queue.db"))


class Config:
    def __init__(self, raw: dict):
        self._raw = raw

        n = raw.get("ntfy", {})
        self.ntfy_base = n.get("base_url", "https://ntfy.sh").rstrip("/")
        self.ntfy_token = os.environ.get("NTFY_TOKEN") or n.get("token")

        t = raw.get("tududi", {})
        self.tududi_base = t.get("base_url", "http://tududi:3002").rstrip("/")
        self.tududi_token = os.environ.get("TUDUDI_API_TOKEN") or t.get("token")
        self.tududi_paths = {
            "create_task": "/api/v1/task",
            "update_task": "/api/v1/task/{id}",
            "list_projects": "/api/v1/projects",
            **t.get("paths", {}),
        }

        o = raw.get("ollama", {})
        self.ollama_base = o.get("base_url", "http://ollama:11434").rstrip("/")
        self.model = o.get("model", "qwen3:30b-a3b")
        self.num_ctx = o.get("num_ctx", 4096)
        self.num_thread = o.get("num_thread", 8)
        self.seed = o.get("seed", 42)
        self.keep_alive = o.get("keep_alive", -1)
        self.request_timeout = o.get("request_timeout", 900)

        w = raw.get("worker", {})
        self.poll_interval = w.get("poll_interval", 10)
        self.max_attempts = w.get("max_attempts", 3)
        self.backoff_base = w.get("backoff_base", 120)
        self.stale_after = w.get("stale_after", 1800)
        self.load_threshold = w.get("load_threshold", 0)  # 0 disables the guard
        self.quiet_hours = w.get("quiet_hours")  # e.g. {"start": 23, "end": 6}

        # topic -> {project_id, notes}
        self.topics = raw.get("topics", {})
        self.default_project_id = raw.get("default_project_id")

    def project_for(self, topic: str):
        entry = self.topics.get(topic) or {}
        return entry.get("project_id", self.default_project_id), entry.get("notes", "")

    @property
    def topic_list(self):
        return sorted(self.topics.keys())


def load() -> Config:
    with open(CONFIG_PATH) as fh:
        return Config(yaml.safe_load(fh) or {})

"""Configuration loading. Env vars override the YAML file for secrets."""
import os
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", "/config/config.yml"))
PROMPT_DIR = Path(os.environ.get("BRIDGE_PROMPTS", "/app/prompts"))
DB_PATH = Path(os.environ.get("BRIDGE_DB", "/data/queue.db"))
REPO_CACHE_DIR = Path(os.environ.get("BRIDGE_REPO_CACHE", "/data/repos"))


class Config:
    def __init__(self, raw: dict):
        self._raw = raw

        n = raw.get("ntfy", {})
        self.ntfy_base = n.get("base_url", "https://ntfy.sh").rstrip("/")
        self.ntfy_token = os.environ.get("NTFY_TOKEN") or n.get("token")
        # Single static topic the planner publishes clarifying questions to and
        # listens for replies on. Same "topic name is a password" rule as topics:.
        self.ntfy_reply_topic = n.get("reply_topic")

        t = raw.get("tududi", {})
        self.tududi_base = t.get("base_url", "http://tududi:3002").rstrip("/")
        self.tududi_token = os.environ.get("TUDUDI_API_TOKEN") or t.get("token")
        self.tududi_paths = {
            "create_task": "/api/v1/task",
            "update_task": "/api/v1/task/{id}",
            "list_projects": "/api/v1/projects",
            "list_tasks": "/api/v1/tasks",
            "get_task": "/api/v1/task/{id}",
            **t.get("paths", {}),
        }
        # Query param used to filter list_tasks by tag. Unconfirmed against a
        # live instance -- override here if it 404s, same as every other path.
        self.tag_query_param = t.get("tag_query_param", "tag")

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

        c = raw.get("claude", {})
        # Deliberately env-var-only, no YAML fallback -- unlike ntfy_token/
        # tududi_token above, which accept either. This secret specifically
        # must come from a plain container environment variable (e.g. an
        # Unraid Docker template's Config entries), never config.yml, so
        # there is exactly one place it can live and one thing to audit.
        self.claude_oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        self.claude_model = c.get("model", "claude-opus-5")
        self.claude_cli_timeout = c.get("cli_timeout", 300)
        self.claude_max_budget_usd = c.get("max_budget_usd", 0.50)

        g = raw.get("github", {})
        self.github_token = os.environ.get("GITHUB_TOKEN") or g.get("token")
        # project_id -> "owner/repo", for local-clone grounding context.
        self.github_repos = {str(k): v for k, v in (g.get("repos") or {}).items()}
        self.repo_cache_dir = REPO_CACHE_DIR

        p = raw.get("planner", {})
        self.planner_trigger_tag = p.get("trigger_tag", "plan-me")
        self.planner_poll_interval = p.get("poll_interval", 60)
        self.planner_max_attempts = p.get("max_attempts", 3)
        self.planner_backoff_base = p.get("backoff_base", 120)
        self.planner_stale_after = p.get("stale_after", 1800)
        # Claude runs remotely (either via the API or, here, the CLI's own
        # subscription-backed inference) -- local box load doesn't gate it
        # by default, unlike worker.load_threshold which guards Ollama's CPU.
        self.planner_load_threshold = p.get("load_threshold", 0)
        self.planner_quiet_hours = p.get("quiet_hours")
        self.max_clarification_rounds = p.get("max_clarification_rounds", 3)
        self.awaiting_input_reminder_after = p.get("awaiting_input_reminder_after", 86400)

    def project_for(self, topic: str):
        entry = self.topics.get(topic) or {}
        return entry.get("project_id", self.default_project_id), entry.get("notes", "")

    @property
    def topic_list(self):
        return sorted(self.topics.keys())

    def notes_for_project(self, project_id) -> str:
        """Reverse-index of topics: (topic -> {project_id, notes}) so the
        planner can reuse PROJECT_NOTES without a second config section
        duplicating project descriptions."""
        project_id = str(project_id) if project_id is not None else None
        for entry in self.topics.values():
            if str(entry.get("project_id")) == project_id:
                return entry.get("notes", "")
        return ""


def load() -> Config:
    with open(CONFIG_PATH) as fh:
        return Config(yaml.safe_load(fh) or {})

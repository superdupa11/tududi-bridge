"""Configuration loading. Env vars override the YAML file for secrets."""
import os
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", "/config/config.yml"))
PROMPT_DIR = Path(os.environ.get("BRIDGE_PROMPTS", "/app/prompts"))
DB_PATH = Path(os.environ.get("BRIDGE_DB", "/data/queue.db"))
REPO_CACHE_DIR = Path(os.environ.get("BRIDGE_REPO_CACHE", "/data/repos"))
# Default for codeserver.workspace_root below -- unlike REPO_CACHE_DIR, this
# one IS also YAML-configurable, since the exact path has to match whatever
# the operator bind-mounted into the code-server container too.
WORKSPACES_DIR = Path(os.environ.get("BRIDGE_WORKSPACES", "/data/workspaces"))


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
        # Separate from the triage settings above -- agentic tool-calling
        # needs far more context than 3-pass classify/draft/critique, so this
        # deliberately does NOT fall back to num_ctx, only to model.
        self.exec_model = o.get("exec_model", self.model)
        self.exec_num_ctx = o.get("exec_num_ctx", 32768)
        self.exec_temperature = o.get("exec_temperature", 0.2)

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

        e = raw.get("executor", {})
        self.executor_trigger_tag = e.get("trigger_tag", "execute-me")
        self.executor_poll_interval = e.get("poll_interval", 60)
        self.executor_max_attempts = e.get("max_attempts", 3)
        self.executor_backoff_base = e.get("backoff_base", 120)
        self.executor_stale_after = e.get("stale_after", 1800)
        # Local inference gates this the same way worker.load_threshold does
        # (the executor's agent loop runs against the same Ollama host) --
        # unlike planner_load_threshold, which stays 0 because Claude runs
        # remotely.
        self.executor_load_threshold = e.get("load_threshold", 0)
        self.executor_quiet_hours = e.get("quiet_hours")
        self.executor_max_steps = e.get("max_steps", 40)
        self.executor_step_timeout = e.get("step_timeout", 120)
        self.executor_total_timeout = e.get("total_timeout", 3600)
        self.executor_allow_push = e.get("allow_push", False)
        # Separate switch from allow_push -- docker build/push are a
        # different capability (image builds, not code pushes) and default
        # off independently. Both are approval-gated when on; see sandbox.py.
        self.executor_allow_docker = e.get("allow_docker", False)
        self.executor_allowed_commands = e.get("allowed_commands") or []
        self.executor_denied_commands = e.get("denied_commands") or []
        # Same nudge mechanic as planner's awaiting_input_reminder_after,
        # applied to parked exec_queue rows (a question or a push/build
        # approval sitting unanswered).
        self.executor_awaiting_input_reminder_after = e.get("awaiting_input_reminder_after", 86400)
        # Defaults to the planner's reply topic -- one ntfy topic for every
        # AI-pipeline notification, unless the operator wants execution
        # results split out separately.
        self.executor_notify_topic = e.get("notify_topic") or self.ntfy_reply_topic

        cs = raw.get("codeserver", {})
        self.exec_backend = cs.get("exec_backend", "docker")
        self.codeserver_container = cs.get("container", "code-server")
        # Path as THIS container sees the shared workspace volume.
        self.workspace_root = Path(cs.get("workspace_root") or WORKSPACES_DIR)
        # Path as code-server sees the SAME bind-mounted volume -- sandbox.py
        # translates between the two for `docker exec -w`. A mismatch here
        # makes every command fail; discover.py's preflight check is the
        # intended early warning.
        self.workspace_container_root = cs.get("workspace_container_root", "/config/workspace")

        # A third, opt-in sandbox backend for projects that need real Apple
        # tooling (Xcode/iOS Simulator) -- structurally impossible inside a
        # Linux container, so those projects route to an actual Mac over SSH
        # instead of code-server. See sandbox.py's module docstring.
        m = raw.get("mac", {})
        self.mac_enabled = m.get("enabled", False)
        self.mac_host = m.get("host")
        self.mac_ssh_user = m.get("ssh_user")
        self.mac_ssh_port = m.get("ssh_port", 22)
        # Path as the bridge container sees the private key file -- mount it
        # in read-only, same pattern as every other credential path here.
        self.mac_ssh_key = m.get("ssh_key")
        # Where synced project clones live on the Mac -- NOT the same tree as
        # workspace_root; a genuinely separate machine, reached over the
        # network, not a shared bind mount like codeserver.workspace_root.
        self.mac_workspace_root = m.get("workspace_root")
        self.mac_connect_timeout = m.get("connect_timeout", 5)
        self.mac_command_timeout = m.get("command_timeout", 900)
        # project_id -> route to the Mac backend for the whole run, but only
        # when mac_reachable() is true at the start of that run; otherwise
        # falls back to exec_backend and the run is flagged as degraded.
        self.mac_projects = {str(p) for p in (m.get("projects") or [])}

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

    def topic_for_project(self, project_id):
        """Same reverse-index as notes_for_project, but returns the topic
        NAME -- how the executor announces a fresh per-run ntfy topic on the
        channel the human already watches for that project. Falls back to
        whichever topic maps to project_id: null (the triage/inbox topic,
        by convention) when project_id is None or unmapped, and finally to
        executor_notify_topic so an announcement is never silently dropped."""
        project_id = str(project_id) if project_id is not None else None
        for topic, entry in self.topics.items():
            if str(entry.get("project_id")) == project_id:
                return topic
        for topic, entry in self.topics.items():
            if entry.get("project_id") is None:
                return topic
        return self.executor_notify_topic


def load() -> Config:
    with open(CONFIG_PATH) as fh:
        return Config(yaml.safe_load(fh) or {})

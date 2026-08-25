"""Headless Claude Code CLI wrapper.

Shells out to the `claude` binary rather than the Anthropic Python SDK,
because the SDK only supports API-key auth -- it cannot run against a Claude
subscription. The CLI can, via a long-lived token from `claude setup-token`
(see config.claude_oauth_token), which is the entire point of this module:
planning runs against subscription usage, not pay-per-token API billing.
"""
import json
import logging
import os
import subprocess

log = logging.getLogger("claude_client")


class ClaudeError(RuntimeError):
    pass


class ClaudeClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def _env(self):
        """Explicit allowlist, never os.environ.copy(). ANTHROPIC_API_KEY /
        ANTHROPIC_AUTH_TOKEN must never reach this subprocess -- Claude
        Code's auth precedence prefers either over CLAUDE_CODE_OAUTH_TOKEN,
        which would silently switch billing to pay-per-token. This is the
        subprocess-level half of that guarantee; planner.py's startup check
        is the complementary process-level half.
        """
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "CLAUDE_CODE_OAUTH_TOKEN": self.cfg.claude_oauth_token or "",
        }

    def chat_json(self, prompt: str, schema: dict, *, effort="high", repo_dir=None):
        """Runs `claude -p` headless with schema-constrained JSON output.

        Returns (structured_output, meta) where meta has "cost_usd" and
        "usage" from the CLI's own response envelope. Raises ClaudeError on
        any failure to obtain valid, schema-shaped output. `repo_dir`, when
        given, is granted via --add-dir so Claude can read it with its own
        Read/Grep/Glob tools (no Bash/Edit/Write access, no prompting --
        --permission-mode dontAsk denies anything outside --allowedTools
        rather than blocking on human approval).
        """
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--effort", effort,
            "--model", self.cfg.claude_model,
            "--permission-mode", "dontAsk",
            "--allowedTools", "Read,Grep,Glob",
            "--no-session-persistence",
            "--max-budget-usd", str(self.cfg.claude_max_budget_usd),
        ]
        if repo_dir:
            cmd += ["--add-dir", str(repo_dir)]

        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.cfg.claude_cli_timeout, env=self._env())
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"claude CLI timed out after {self.cfg.claude_cli_timeout}s") from e
        except OSError as e:
            raise ClaudeError(f"failed to launch claude CLI: {e}") from e

        if r.returncode != 0:
            raise ClaudeError(f"claude CLI exited {r.returncode}: "
                              f"{(r.stderr or r.stdout)[-500:]}")

        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeError(f"claude CLI did not return valid JSON: {r.stdout[-500:]}") from e

        if data.get("is_error"):
            raise ClaudeError(f"claude CLI reported an error ({data.get('subtype')}): "
                              f"{str(data.get('result'))[:500]}")

        structured = data.get("structured_output")
        if structured is None:
            raise ClaudeError(f"no structured_output in claude CLI response: {r.stdout[-500:]}")

        return structured, {"cost_usd": data.get("total_cost_usd"), "usage": data.get("usage")}

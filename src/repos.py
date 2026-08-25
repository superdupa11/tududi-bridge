"""Local repo clone management for GitHub grounding context.

Instead of a custom GitHub REST client pre-fetching README/file-listing text
into the prompt, the planner clones each project's repo locally and lets
Claude explore it itself with its own Read/Grep/Glob tools (see
claude_client.py's --add-dir). Simpler than the REST-client alternative: no
API pagination, no rate-limit budget, no README-endpoint parsing.
"""
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("repos")


class RepoError(RuntimeError):
    pass


def _clone_url(cfg, owner_repo):
    if cfg.github_token:
        return f"https://x-access-token:{cfg.github_token}@github.com/{owner_repo}.git"
    return f"https://github.com/{owner_repo}.git"


def ensure_repo_clone(cfg, project_id) -> Path | None:
    """Looks up cfg.github_repos.get(project_id) -> 'owner/repo'. Returns None
    if unmapped, or on any clone failure -- soft-fail, since the caller plans
    without local grounding rather than failing the whole task.

    Always does a fresh shallow clone (--depth 1), discarding any previous
    copy first, rather than an incremental fetch/reset -- avoids needing to
    detect the remote's default branch name, and a shallow clone of a small
    project repo is cheap enough to redo per planning pass.
    """
    owner_repo = cfg.github_repos.get(str(project_id))
    if not owner_repo:
        return None

    dest = cfg.repo_cache_dir / owner_repo.replace("/", "__")
    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch",
             _clone_url(cfg, owner_repo), str(dest)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RepoError(r.stderr[-400:])
        # The token-in-URL auth is only needed for the clone itself -- scrub
        # it from .git/config immediately so it doesn't linger on disk for
        # however long this clone directory sticks around.
        if cfg.github_token:
            subprocess.run(
                ["git", "remote", "set-url", "origin", f"https://github.com/{owner_repo}.git"],
                cwd=dest, capture_output=True, text=True, timeout=10,
            )
        return dest
    except (RepoError, OSError, subprocess.TimeoutExpired) as e:
        log.warning("clone failed for %s (planning without grounding): %s", owner_repo, e)
        return None

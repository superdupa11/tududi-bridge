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


def ensure_workspace_clone(cfg, project_id) -> Path:
    """Like ensure_repo_clone(), but for the executor -- and NOT interchangeable
    with it. ensure_repo_clone() rmtree()s its destination on every planning
    pass (cfg.repo_cache_dir is meant to be disposable grounding context), so
    the executor's live workspace -- possibly mid-agent-run, uncommitted work
    included -- must live under cfg.workspace_root instead, a directory this
    function never deletes.

    Does a full (non-shallow) clone so branching/committing behave normally
    (a --depth 1 clone can't push branches cleanly). Raises RepoError if the
    project has no repo mapping, the clone fails, or an existing workspace is
    dirty (never silently discards in-progress agent work).
    """
    owner_repo = cfg.github_repos.get(str(project_id))
    if not owner_repo:
        raise RepoError(f"no repo mapped for project {project_id!r} under github.repos")

    dest = cfg.workspace_root / owner_repo.replace("/", "__")

    if dest.exists():
        r = subprocess.run(["git", "status", "--porcelain"], cwd=dest,
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RepoError(f"{dest} exists but is not a usable git tree: {r.stderr[-400:]}")
        if r.stdout.strip():
            raise RepoError(f"{dest} has uncommitted changes -- refusing to reuse or "
                            "delete it; resolve manually")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "clone", _clone_url(cfg, owner_repo), str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RepoError(f"workspace clone failed for {owner_repo}: {r.stderr[-400:]}")
    if cfg.github_token:
        subprocess.run(
            ["git", "remote", "set-url", "origin", f"https://github.com/{owner_repo}.git"],
            cwd=dest, capture_output=True, text=True, timeout=10,
        )
    # A bare container image has no ~/.gitconfig -- git commit still succeeds
    # (recent git auto-detects a fallback identity from $USER@hostname and
    # warns on stderr), but that fallback is a meaningless container hostname,
    # not something anyone reviewing `git log` should have to decode. Set a
    # real, local (this clone only) identity instead of relying on the
    # fallback or requiring global container-wide git config.
    subprocess.run(["git", "config", "user.name", "tududi-executor"],
                   cwd=dest, capture_output=True, text=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "tududi-executor@localhost"],
                   cwd=dest, capture_output=True, text=True, timeout=10)
    return dest

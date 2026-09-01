"""Pluggable command-execution backend for agent.py's `run` tool.

Three backends. Which one a given RUN uses is resolved once by executor.py
(passed as `backend` to run()/preflight-adjacent mac_reachable() below) --
NOT read fresh from cfg.exec_backend on every call, because a project can be
Mac-routed only when the Mac happens to be reachable, and that decision must
stay fixed for the whole run rather than flip mid-flight if the Mac drops
off the network partway through (see executor.py's run_backend column).

  "docker" -- `docker exec` into the code-server container (cfg.codeserver_
              container) over the mounted docker.sock, so commands run
              against the project's real toolchain and are visible live in
              the code-server terminal. This is what makes the feature's
              point -- "the human watches it happen live in code-server" --
              actually true. The default backend (cfg.exec_backend).
  "local"  -- runs directly inside this container's own shell, against the
              same workspace path. No docker.sock needed; used for testing,
              or for deployments that would rather not grant the bridge
              container that access.
  "mac"    -- ssh + rsync to a Mac (cfg.mac_*) for projects that need real
              Apple tooling (Xcode/iOS Simulator) that simply cannot exist
              inside a Linux container. Only ever selected for project_ids
              listed in cfg.mac_projects, and only when mac_reachable(cfg)
              was true at the start of the run -- otherwise executor.py
              falls back to cfg.exec_backend and flags the run as degraded.
              The workspace is mirrored (rsync, both directions) around
              every mac-backend command, not just once per run: the local
              copy is what code-server and the eventual git commit see, so
              anything the Mac side produces (a regenerated lockfile, e.g.)
              needs to come back before that command's result is trusted.

A workspace root with a `.venv/bin/activate` (see _venv_activate_prefix) gets
it sourced ahead of every command on the "docker"/"local" backends -- so a
project gets its own isolated Python environment (create one with `python3
-m venv .venv`) instead of needing its dependencies baked into the shared
code-server image, which doesn't scale across projects with conflicting
version needs. Same idea for Node via `.nvmrc`/`.node-version` (see
_node_activate_prefix) -- nvm is sourced and pointed at the pinned version,
installing it first if this machine doesn't have it yet, rather than every
project sharing whatever single Node build happens to be on PATH. Neither is
applied to "mac": a venv or nvm-installed Node built here is a Linux one,
and activating either against the real Mac's interpreter over SSH would be
worse than not activating anything.

Every backend runs through the same policy gate (allowed/denied command
prefixes, plus special handling for `git push` / `docker build` / `docker
push`) before dispatch. A denied command returns a refusal string, same as
before. A `git push` (when executor.allow_push is set) or `docker build`/
`docker push` (when executor.allow_docker is set) doesn't run immediately
either -- it comes back as the NEEDS_APPROVAL sentinel instead, which
agent.py turns into a pause for human approval over the run's ntfy topic.
Every other `docker ...` subcommand stays denied outright: there's no
approval path for e.g. `docker rm`/`docker exec` from inside the agent's own
shell, only for the two operations a project's own build/verify step would
plausibly need.
"""
import logging
import re
import shlex
import socket
import subprocess
from pathlib import Path

log = logging.getLogger("sandbox")


class SandboxError(RuntimeError):
    pass

MAX_OUTPUT = 4000  # chars per stream -- keeps transcripts/notes bounded

# Sentinel exit code sandbox.run() returns instead of actually executing a
# command that needs human approval first -- stdout carries the command
# text, stderr carries the reason, matching the normal (code, stdout,
# stderr) shape so callers don't need a different return type to check for.
NEEDS_APPROVAL = -100

# Always denied, regardless of cfg.executor_denied_commands. Bare `docker`
# is intentionally NOT here -- see _check_policy, which allows only
# build/push (and only with executor.allow_docker set), gated by approval.
DEFAULT_DENIED_PREFIXES = ("sudo", "rm -rf /")

_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")
_DOCKER_BUILD_PUSH_RE = re.compile(r"^\s*docker\s+(buildx\s+build|build|push)\b")


def _truncate(s: str) -> str:
    if len(s) <= MAX_OUTPUT:
        return s
    return s[:MAX_OUTPUT] + f"\n...[truncated, {len(s) - MAX_OUTPUT} more chars]"


def _translate_path(cfg, path: str) -> str:
    """workspace_root (this container's view) -> workspace_container_root
    (code-server's view of the same bind-mounted volume)."""
    root = str(cfg.workspace_root)
    if path == root:
        return cfg.workspace_container_root
    if path.startswith(root + "/"):
        return cfg.workspace_container_root + path[len(root):]
    return path


def mac_reachable(cfg) -> bool:
    """Cheap up-front check -- a raw TCP connect to the Mac's SSH port, not a
    full SSH handshake -- so a run can decide "mac" vs. fall back to
    cfg.exec_backend before touching the workspace at all. False whenever
    mac.enabled is off or mac.host isn't set, so callers never need to
    separately check cfg.mac_enabled first."""
    if not (cfg.mac_enabled and cfg.mac_host):
        return False
    try:
        with socket.create_connection((cfg.mac_host, cfg.mac_ssh_port),
                                      timeout=cfg.mac_connect_timeout):
            return True
    except OSError:
        return False


def _mac_ssh_base(cfg):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
          "-o", f"ConnectTimeout={cfg.mac_connect_timeout}", "-p", str(cfg.mac_ssh_port)]
    if cfg.mac_ssh_key:
        cmd += ["-i", cfg.mac_ssh_key]
    cmd.append(f"{cfg.mac_ssh_user}@{cfg.mac_host}")
    return cmd


def _mac_rsync_ssh_opt(cfg):
    opt = f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p {cfg.mac_ssh_port}"
    if cfg.mac_ssh_key:
        opt += f" -i {cfg.mac_ssh_key}"
    return opt


def _mac_remote_dir(cfg, local_workspace_dir: str) -> str:
    """The synced copy's path on the Mac -- same repo-slug directory name
    (owner__repo) as the local workspace, just rooted under
    cfg.mac_workspace_root instead of cfg.workspace_root."""
    repo_slug = Path(local_workspace_dir).name
    return f"{cfg.mac_workspace_root.rstrip('/')}/{repo_slug}"


def _mac_sync(cfg, local_dir: str, remote_dir: str, *, direction: str):
    """direction 'to' mirrors local -> Mac (before running a command);
    'from' mirrors Mac -> local (after), respecting the repo's own
    .gitignore on the way back so Xcode/CocoaPods/DerivedData build junk
    doesn't get pulled into the workspace code-server and git see -- only
    files the project itself already tracks (or would track) come back."""
    remote = f"{cfg.mac_ssh_user}@{cfg.mac_host}:{remote_dir}/"
    args = ["rsync", "-az", "-e", _mac_rsync_ssh_opt(cfg)]
    if direction == "to":
        args += ["--delete", f"{local_dir}/", remote]
    else:
        gitignore = Path(local_dir) / ".gitignore"
        if gitignore.exists():
            args += ["--filter=:- .gitignore"]
        args += [remote, f"{local_dir}/"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=cfg.mac_command_timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SandboxError(f"rsync {direction} Mac failed: {e}") from e
    if r.returncode != 0:
        raise SandboxError(f"rsync {direction} Mac failed: {r.stderr[-400:]}")


def _run_mac(cfg, command: str, *, cwd: str, timeout: int):
    remote_dir = _mac_remote_dir(cfg, cwd)
    try:
        _mac_sync(cfg, cwd, remote_dir, direction="to")
        ssh_cmd = _mac_ssh_base(cfg) + [f"cd {shlex.quote(remote_dir)} && {command}"]
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        _mac_sync(cfg, cwd, remote_dir, direction="from")
    except SandboxError as e:
        return 1, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s"
    except OSError as e:
        return 1, "", f"failed to launch ssh: {e}"
    return r.returncode, _truncate(r.stdout), _truncate(r.stderr)


def mac_status(cfg) -> str:
    """Informational only (never fails discover.py's overall ok flag) -- the
    Mac being offline is an expected, handled condition, not misconfiguration."""
    if not cfg.mac_enabled:
        return "disabled"
    if not cfg.mac_host:
        return "enabled but mac.host is not set"
    return f"{cfg.mac_host}:{cfg.mac_ssh_port} " + ("reachable" if mac_reachable(cfg) else
                                                     "NOT reachable (mac-routed projects will "
                                                     "fall back to the default backend)")


def _venv_activate_prefix(cwd: str) -> str:
    """A `source .venv/bin/activate && ` prefix if the workspace root has a
    project-local venv, else "". Only used for the "docker"/"local" backends
    (see run()) -- both execute against this container's own filesystem view
    of the bind-mounted workspace, same as the plain stat here, so no extra
    round-trip is needed to check. The sourced path stays relative so it
    resolves correctly wherever the command actually runs (this container
    for "local", or code-server for "docker" -- `-w` already points bash at
    the same mounted directory).

    Deliberately NOT applied to the "mac" backend: a venv created here is a
    Linux venv (this container's python3), and activating it on the actual
    Mac over SSH would point at an interpreter path that doesn't exist
    there -- worse than no venv at all, not just unhelpful.

    This is the fix for the class of failure preflight's toolchain_report()
    surfaces (pip/npm missing from code-server's PATH): rather than every
    project needing its dependencies baked into the shared code-server
    image, each project gets its own venv at its workspace root, and every
    command run against it picks the venv's pip/python up automatically."""
    if (Path(cwd) / ".venv" / "bin" / "activate").is_file():
        return "source .venv/bin/activate && "
    return ""


def _node_activate_prefix(cwd: str) -> str:
    """Node's equivalent of _venv_activate_prefix above: if the workspace
    root pins a version via `.nvmrc`/`.node-version` (nvm's own two
    supported files -- picked so this recognizes a project's *existing*
    convention rather than requiring one invented for this bridge), source
    nvm and `nvm use` (installing that version first if this machine
    doesn't have it yet) before the command. No such file -> no prefix, same
    as today: whatever `node`/`npm` resolve to on PATH already.

    Unlike a venv, nvm is a shell *function*, not a binary on PATH, so it
    has to be re-sourced in every non-interactive `bash -lc` rather than
    just needing its bin/ directory prepended once -- and unlike checking
    for `.venv/bin/activate` (always on the shared workspace mount, visible
    from this process), whether nvm itself is installed can only be checked
    from wherever the command actually runs (code-server, for the "docker"
    backend, a container this process can't stat into), so that check has
    to happen inside the same shell script, at run time, not here in
    Python. The whole `nvm use`/`nvm install` step is therefore gated on
    nvm.sh actually existing there -- if it doesn't (nvm was never set up
    in code-server), this silently no-ops down to whatever `node`/`npm`
    already resolve to on PATH, same as if the project had no pinned
    version at all, rather than spamming every command's stderr with
    "nvm: command not found". When nvm.sh *does* exist, `nvm use`'s output
    (or the fallback `nvm install`'s) is deliberately left unsuppressed --
    installing a version this machine has never used before downloads a
    real Node build, which can be slow, and silently swallowing that means
    a slow/failed install looks identical to a fast/successful one. Neither
    call is chained with `&&` into the real command, though: if both fail
    (bad version, no network, ...) the command still runs against whatever
    `node`/`npm` happen to be on PATH -- a transient install hiccup
    shouldn't hard-block every command in the chunk.

    Requires nvm itself already installed in code-server (one-time setup,
    same as python3-venv for _venv_activate_prefix) -- see
    https://github.com/nvm-sh/nvm#install--update-script. Not applied to
    "mac", same reasoning as _venv_activate_prefix."""
    if not any((Path(cwd) / name).is_file() for name in (".nvmrc", ".node-version")):
        return ""
    return ('export NVM_DIR="$HOME/.nvm"; '
           'if [ -s "$NVM_DIR/nvm.sh" ]; then \\. "$NVM_DIR/nvm.sh"; nvm use || nvm install; fi; ')


def _refused(reason: str):
    log.warning("refused command: %s", reason)
    return (1, "", f"refused: {reason}")


def _check_policy(cfg, command: str):
    """Returns (action, detail): action is "allow" (detail None), "deny"
    (detail is the refusal reason), or "approve" (detail is a human-readable
    description of what needs approving)."""
    stripped = command.strip()

    if cfg.executor_allowed_commands:
        if not any(stripped.startswith(p) for p in cfg.executor_allowed_commands):
            return "deny", f"{stripped[:60]!r} does not match any allowed_commands prefix"

    for p in list(cfg.executor_denied_commands) + list(DEFAULT_DENIED_PREFIXES):
        if stripped.startswith(p):
            return "deny", f"{stripped[:60]!r} matches denied prefix {p!r}"

    if _GIT_PUSH_RE.search(stripped):
        if not cfg.executor_allow_push:
            return "deny", "git push is disabled (executor.allow_push is false)"
        return "approve", f"git push: `{stripped}`"

    if _DOCKER_BUILD_PUSH_RE.match(stripped):
        if not cfg.executor_allow_docker:
            return "deny", "docker build/push is disabled (executor.allow_docker is false)"
        return "approve", f"docker build/push: `{stripped}`"

    if stripped.startswith("docker"):
        return "deny", (f"{stripped[:60]!r} matches denied prefix 'docker' "
                        "(only build/push can be approved)")

    return "allow", None


# Toolchains sandbox.run() commands routinely need -- checked with the same
# `bash -lc` (or ssh, for "mac") a real run would use, so a login-shell PATH
# gap (e.g. nvm/pyenv init lines that only ever landed in .bashrc, which a
# non-interactive login shell doesn't source) shows up here instead of
# partway through an unattended overnight run. Informational only, like
# mac_status() below -- not every project needs every tool, so a gap here
# doesn't fail discover.py's overall ok flag.
TOOLCHAIN_PROBES = ("git", "python3", "pip3", "node", "npm", "nvm")


def _toolchain_probe_command() -> str:
    checks = [
        f"if command -v {t} >/dev/null 2>&1; then echo {t}:ok; else echo {t}:missing; fi"
        for t in TOOLCHAIN_PROBES if t != "nvm"
    ]
    # nvm is a shell *function*, not a PATH binary (see
    # _node_activate_prefix's docstring above) -- a plain `command -v nvm`
    # never finds it even when installed, so it has to be sourced first,
    # the same way a real run does.
    checks.append(
        'export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && \\. "$NVM_DIR/nvm.sh"; '
        'if command -v nvm >/dev/null 2>&1; then echo nvm:ok; else echo nvm:missing; fi'
    )
    return " ; ".join(checks)


def toolchain_report(cfg, backend=None) -> str:
    """Runs a `command -v` probe for TOOLCHAIN_PROBES through the given
    backend (default cfg.exec_backend) -- the exact way sandbox.run() would
    dispatch a command on that backend -- and summarizes what's missing.
    Never raises; a probe that can't even run comes back as text, same as
    mac_status()."""
    backend = backend or cfg.exec_backend
    probe = _toolchain_probe_command()

    if backend == "docker":
        argv = ["docker", "exec", cfg.codeserver_container, "bash", "-lc", probe]
    elif backend == "local":
        argv = ["bash", "-lc", probe]
    elif backend == "mac":
        if not mac_reachable(cfg):
            return "skipped (Mac not reachable)"
        argv = _mac_ssh_base(cfg) + [probe]
    else:
        return f"skipped (unknown backend {backend!r})"

    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"probe failed: {e}"

    found = {}
    for line in (r.stdout or "").strip().splitlines():
        tool, _, status = line.partition(":")
        if tool in TOOLCHAIN_PROBES:
            found[tool] = status.strip() == "ok"

    missing = [t for t in TOOLCHAIN_PROBES if not found.get(t)]
    if missing:
        present = [t for t in TOOLCHAIN_PROBES if found.get(t)]
        detail = f"missing on PATH via `bash -lc`: {', '.join(missing)}"
        if present:
            detail += f" (present: {', '.join(present)})"
        return detail
    return f"all present via `bash -lc`: {', '.join(TOOLCHAIN_PROBES)}"


def preflight(cfg):
    """Verifies the configured backend actually works. Returns (ok, detail)."""
    if cfg.exec_backend == "docker":
        try:
            r = subprocess.run(
                ["docker", "exec", cfg.codeserver_container, "true"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"docker exec failed: {e}"
        if r.returncode != 0:
            return False, (f"docker exec {cfg.codeserver_container} true -> "
                           f"{r.returncode}: {(r.stderr or '').strip()[:300]}")
        return True, f"docker exec into {cfg.codeserver_container!r} OK"
    if cfg.exec_backend == "local":
        return True, "local backend, no preflight needed"
    return False, f"unknown exec_backend: {cfg.exec_backend!r}"


def run(cfg, command: str, *, cwd: str, timeout: int, pre_approved: bool = False, backend=None):
    """Returns (exit_code, stdout, stderr), each stream truncated to
    MAX_OUTPUT chars -- except when the command needs human approval, in
    which case exit_code is the NEEDS_APPROVAL sentinel, stdout is the
    original command text, and stderr is the reason, and nothing runs.

    `backend` overrides cfg.exec_backend for this one call -- executor.py
    resolves and persists a single backend ("docker"/"local"/"mac") for the
    whole run once, at the start (see module docstring), and passes it
    through explicitly rather than this function re-reading cfg.exec_backend
    fresh each time, which would let an unrelated concurrent task's config
    change (or, for "mac", a reachability flap) leak into this run.

    `pre_approved` skips the approve-vs-deny distinction (a plain "allow"
    command ignores it) -- executor.py sets it when re-running a command the
    human already said yes to, so this doesn't re-park on the same command
    forever. It does NOT skip the deny checks: an operator-configured denied
    prefix or a disabled allow_push/allow_docker flag still wins even if a
    human somehow approved it (those flags are the actual on/off switch;
    approval only gates commands the switch already permits).
    """
    backend = backend or cfg.exec_backend

    action, detail = _check_policy(cfg, command)
    if action == "deny":
        return _refused(detail)
    if action == "approve" and not pre_approved:
        log.info("command needs human approval: %s", detail)
        return NEEDS_APPROVAL, command, detail

    if backend == "mac":
        return _run_mac(cfg, command, cwd=cwd, timeout=timeout)
    if backend == "docker":
        shell_command = _venv_activate_prefix(cwd) + _node_activate_prefix(cwd) + command
        argv = ["docker", "exec", "-w", _translate_path(cfg, cwd),
                cfg.codeserver_container, "bash", "-lc", shell_command]
        run_cwd = None
    elif backend == "local":
        shell_command = _venv_activate_prefix(cwd) + _node_activate_prefix(cwd) + command
        argv = ["bash", "-lc", shell_command]
        run_cwd = cwd
    else:
        return _refused(f"unknown backend: {backend!r}")

    try:
        r = subprocess.run(argv, cwd=run_cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s"
    except OSError as e:
        return 1, "", f"failed to launch: {e}"

    return r.returncode, _truncate(r.stdout), _truncate(r.stderr)

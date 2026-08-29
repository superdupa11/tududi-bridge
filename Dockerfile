FROM python:3.12-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1 \
    BRIDGE_CONFIG=/config/config.yml \
    BRIDGE_PROMPTS=/app/prompts \
    BRIDGE_DB=/data/queue.db

# ---------------------------------------------------------------------------
# runtime: lean image for ingest/worker -- pip deps only, no git, no Claude CLI.
# Source is copied last so an ordinary code change only invalidates this one
# layer, not the pip-install layer above it.
# ---------------------------------------------------------------------------
FROM base AS runtime

COPY src/ /app/
COPY prompts/ /app/prompts/

CMD ["python", "worker.py"]

# ---------------------------------------------------------------------------
# planner-tools: git (for repo-grounding clones) + the Claude Code CLI
# (installs as a self-contained native binary, no Node.js needed). Built
# from `base` -- deliberately NOT from `runtime` -- and before any source
# code is copied in, so this layer is cached across every build that only
# touches src/ or prompts/; it only re-runs if requirements.txt or this
# RUN line itself changes.
#
# Auth is via CLAUDE_CODE_OAUTH_TOKEN at runtime (see config.example.yml) --
# `claude login`/`claude setup-token` is never run inside the image, so no
# credentials file ever lands on disk here; that's deliberate, see
# planner.py's assert_no_metered_billing_vars().
# ---------------------------------------------------------------------------
FROM base AS planner-tools

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/root/.local/bin:$PATH \
    BRIDGE_REPO_CACHE=/data/repos

# ---------------------------------------------------------------------------
# planner: planner-tools + source, copied last for the same cache-locality
# reason as runtime above -- a code change never re-triggers apt-get/curl.
# ---------------------------------------------------------------------------
FROM planner-tools AS planner

COPY src/ /app/
COPY prompts/ /app/prompts/

CMD ["python", "planner.py"]

# ---------------------------------------------------------------------------
# executor-tools: git (workspace clones, via repos.ensure_workspace_clone) +
# the docker CLI (so sandbox.py's docker backend can `docker exec` into the
# code-server container over the mounted socket) + openssh-client/rsync (so
# sandbox.py's optional "mac" backend can reach a real Mac for projects that
# need Apple tooling -- Xcode/iOS Simulator -- no Linux container can
# provide; see config.example.yml's `mac:` section). Deliberately NOT built
# from planner-tools -- no Claude CLI here, executor.py never shells out to
# `claude` (see executor.py's module docstring). Built from `base`, before
# source is copied, for the same cache-locality reason as planner-tools.
#
# The docker CLI here only ever talks to a socket mounted in from the host
# (see run.sh) -- no dockerd runs inside this image.
# ---------------------------------------------------------------------------
FROM base AS executor-tools

RUN apt-get update && apt-get install -y --no-install-recommends \
        git docker.io openssh-client rsync ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV BRIDGE_REPO_CACHE=/data/repos \
    BRIDGE_WORKSPACES=/data/workspaces

# ---------------------------------------------------------------------------
# executor: executor-tools + source, copied last for the same cache-locality
# reason as planner above.
# ---------------------------------------------------------------------------
FROM executor-tools AS executor

COPY src/ /app/
COPY prompts/ /app/prompts/

CMD ["python", "executor.py"]

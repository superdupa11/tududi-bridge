FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1 \
    BRIDGE_CONFIG=/config/config.yml \
    BRIDGE_PROMPTS=/app/prompts \
    BRIDGE_DB=/data/queue.db \
    BRIDGE_REPO_CACHE=/data/repos \
    BRIDGE_WORKSPACES=/data/workspaces

# ---------------------------------------------------------------------------
# One image for all four daemons (ingest/worker/planner/executor) --
# differentiated at `docker run` time by a trailing command argument
# (e.g. `python executor.py`; see run.sh and both unraid-template*.xml
# files' PostArgs), not by which image you picked. Used to be three images
# instead of one (a lean one for ingest/worker, plus tools-heavy ones for
# planner and executor) purely to keep git/the Claude CLI/the docker CLI/
# ssh/rsync out of containers that never touch them. That isolation was
# real but modest -- the actual privilege boundary is whatever gets
# bind-mounted at `docker run` time (docker.sock, an SSH key), not whether
# a binary happens to be present in the image -- so for a single-operator
# host this tradeoff favors one image to build and reason about over three.
#
# git: repo/workspace clones (repos.py, both ensure_repo_clone and
# ensure_workspace_clone). docker.io: sandbox.py's docker backend
# (`docker exec` into code-server over a mounted socket -- the CLI here
# only ever talks to a socket mounted in from the host, no dockerd runs in
# this image). openssh-client + rsync: sandbox.py's optional mac backend.
# curl + the Claude CLI install: planner.py's headless `claude -p` calls.
#
# Auth for the Claude CLI is via CLAUDE_CODE_OAUTH_TOKEN at runtime (see
# config.example.yml) -- `claude login`/`claude setup-token` is never run
# inside the image, so no credentials file ever lands on disk here;
# planner.py's assert_no_metered_billing_vars() is the runtime half of
# that guarantee. Every daemon's image now carries the Claude CLI and the
# docker CLI regardless of whether that daemon uses them -- harmless for
# the ones that don't (ingest/worker never invoke `claude` or `docker`, and
# a CLI with no socket/credentials behind it can't do anything), but worth
# knowing the isolation is by mount and by code, not by image, now.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        git docker.io openssh-client rsync curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/root/.local/bin:$PATH

# Source copied last so an ordinary code change only invalidates this one
# layer, not the apt-get/pip layers above it.
COPY src/ /app/
COPY prompts/ /app/prompts/

CMD ["python", "worker.py"]

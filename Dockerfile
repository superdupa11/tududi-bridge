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

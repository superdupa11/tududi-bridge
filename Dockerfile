FROM python:3.12-slim AS runtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/
COPY prompts/ /app/prompts/

ENV PYTHONUNBUFFERED=1 \
    BRIDGE_CONFIG=/config/config.yml \
    BRIDGE_PROMPTS=/app/prompts \
    BRIDGE_DB=/data/queue.db

CMD ["python", "worker.py"]

# ---------------------------------------------------------------------------
# Planner stage: adds git (for repo-grounding clones) and the Claude Code CLI
# (installs as a self-contained native binary, no Node.js needed) on top of
# the same base. ingest/worker keep using the lean `runtime` image above --
# only the planner needs this heavier one.
#
# Auth is via CLAUDE_CODE_OAUTH_TOKEN at runtime (see config.example.yml) --
# `claude login`/`claude setup-token` is never run inside the image, so no
# credentials file ever lands on disk here; that's deliberate, see
# planner.py's assert_no_metered_billing_vars().
# ---------------------------------------------------------------------------
FROM runtime AS planner

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/root/.local/bin:$PATH \
    BRIDGE_REPO_CACHE=/data/repos

CMD ["python", "planner.py"]

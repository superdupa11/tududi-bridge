#!/usr/bin/env bash
# Build the image(s) and (re)start the ingest + worker + planner containers
# with plain `docker build`/`docker run` -- no docker-compose involved.
#
# Usage: ./run.sh
#
# Config/data live outside the repo, at $APPDATA_DIR (default matches the
# path used throughout README.md). Override by exporting APPDATA_DIR first.
#
# The planner container needs CLAUDE_CODE_OAUTH_TOKEN in .env (from running
# `claude setup-token` once on a machine with an active Claude subscription)
# and, optionally, GITHUB_TOKEN for private repo grounding. Do NOT put
# ANTHROPIC_API_KEY in .env or anywhere else this container's environment is
# built from -- the planner refuses to start if it sees one, since Claude
# Code would otherwise silently prefer it over the subscription token and
# switch billing to pay-per-token.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE="tududi-bridge:latest"
PLANNER_IMAGE="tududi-bridge-planner:latest"
APPDATA_DIR="${APPDATA_DIR:-/mnt/user/appdata/tududi-bridge}"
CONFIG_DIR="$APPDATA_DIR/config"
DATA_DIR="$APPDATA_DIR/data"
ENV_FILE="$SCRIPT_DIR/.env"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$DATA_DIR/repos"

if [ ! -f "$CONFIG_DIR/config.yml" ]; then
  echo "missing $CONFIG_DIR/config.yml -- copy config.example.yml there and edit it first" >&2
  exit 1
fi

ENV_FILE_ARGS=()
if [ -f "$ENV_FILE" ]; then
  ENV_FILE_ARGS=(--env-file "$ENV_FILE")
else
  echo "no .env found at $ENV_FILE -- TUDUDI_API_TOKEN must already be exported" >&2
fi

echo "== build =="
docker build --pull --rm --target runtime -t "$IMAGE" .
docker build --rm --target planner -t "$PLANNER_IMAGE" .

echo "== restart containers =="
docker rm -f tududi-ingest tududi-worker tududi-planner >/dev/null 2>&1 || true

docker run -d \
  --name tududi-ingest \
  --restart unless-stopped \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  "$IMAGE" python ingest.py

docker run -d \
  --name tududi-worker \
  --restart unless-stopped \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  -v "$SCRIPT_DIR/prompts:/app/prompts:ro" \
  "$IMAGE" python worker.py

docker run -d \
  --name tududi-planner \
  --restart unless-stopped \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  -v "$SCRIPT_DIR/prompts:/app/prompts:ro" \
  "$PLANNER_IMAGE" python planner.py

echo "== done =="
docker ps --filter name=tududi-ingest --filter name=tududi-worker --filter name=tududi-planner

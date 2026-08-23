#!/usr/bin/env bash
# Build the image and (re)start the ingest + worker containers with plain
# `docker build`/`docker run` -- no docker-compose involved.
#
# Usage: ./run.sh
#
# Config/data live outside the repo, at $APPDATA_DIR (default matches the
# path used throughout README.md). Override by exporting APPDATA_DIR first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE="tududi-bridge:latest"
APPDATA_DIR="${APPDATA_DIR:-/mnt/user/appdata/tududi-bridge}"
CONFIG_DIR="$APPDATA_DIR/config"
DATA_DIR="$APPDATA_DIR/data"
ENV_FILE="$SCRIPT_DIR/.env"

mkdir -p "$CONFIG_DIR" "$DATA_DIR"

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
docker build --pull --rm -t "$IMAGE" .

echo "== restart containers =="
docker rm -f tududi-ingest tududi-worker >/dev/null 2>&1 || true

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

echo "== done =="
docker ps --filter name=tududi-ingest --filter name=tududi-worker

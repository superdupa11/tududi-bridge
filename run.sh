#!/usr/bin/env bash
# Build one image and (re)start the ingest + worker + planner + executor
# containers with plain `docker build`/`docker run` -- no docker-compose
# involved. All four containers run the same image; which daemon each one
# runs is just the trailing `python <file>.py` argument on its `docker run`.
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
#
# The executor container mounts /var/run/docker.sock so its default "docker"
# sandbox backend can `docker exec` into your code-server container -- that
# grants tududi-executor root-equivalent control of every container on this
# host, not just code-server. If that's not acceptable, set
# codeserver.exec_backend: local in config.yml and drop the docker.sock
# mount below yourself. Either way, WORKSPACES_DIR below must ALSO be
# bind-mounted into your code-server container, at the path configured as
# codeserver.workspace_container_root in config.yml -- both containers need
# to see the same clones for the docker backend's path translation to work.
#
# Optional: projects listed under `mac.projects` in config.yml (e.g. an iOS/
# Flutter project that needs real Xcode/Simulator tooling no Linux container
# can provide) route to a Mac over SSH instead. Not wired into the docker run
# below by default -- if you enable `mac:` in config.yml, also add a
# read-only mount for the private key at whatever path you set as
# mac.ssh_key, e.g.:
#   -v "$HOME/.ssh/tududi_mac_key:/config/mac_ssh_key:ro" \
# (a `-v` for a file that doesn't exist yet creates an empty directory
# there instead, which is why this isn't uncommented by default.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE="tududi-bridge:latest"
APPDATA_DIR="${APPDATA_DIR:-/mnt/user/appdata/tududi-bridge}"
CONFIG_DIR="$APPDATA_DIR/config"
DATA_DIR="$APPDATA_DIR/data"
WORKSPACES_DIR="$APPDATA_DIR/workspaces"
ENV_FILE="$SCRIPT_DIR/.env"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$DATA_DIR/repos" "$WORKSPACES_DIR"

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
docker rm -f tududi-ingest tududi-worker tududi-planner tududi-executor >/dev/null 2>&1 || true

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
  "$IMAGE" python planner.py

docker run -d \
  --name tududi-executor \
  --restart unless-stopped \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  -v "$WORKSPACES_DIR:/data/workspaces" \
  -v "$SCRIPT_DIR/prompts:/app/prompts:ro" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "$IMAGE" python executor.py

echo "== done =="
docker ps --filter name=tududi-ingest --filter name=tududi-worker --filter name=tududi-planner \
  --filter name=tududi-executor

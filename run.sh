#!/usr/bin/env bash
# Build one image, push it to Docker Hub, and (re)start the ingest + worker
# + planner + executor containers locally from it -- plain `docker build`/
# `docker push`/`docker run`, no docker-compose involved. All four
# containers run the same image; which daemon each one runs is just the
# trailing `python <file>.py` argument on its `docker run` (Unraid:
# PostArgs in unraid-template.xml / unraid-template-executor.xml).
#
# The push step means Unraid's own "Force Update" button on tududi-planner/
# tududi-executor (built from those two templates, Repository set to the
# same $IMAGE tag) also picks up whatever you just built here, without
# needing to run this script's local `docker run` blocks below at all --
# both paths end up in sync since they come from the same build+push.
# Override IMAGE if brianjwalz/tududi-bridge:latest isn't your Docker Hub
# namespace, and make sure `docker login` has already been done for it.
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
#
# Each container below has a --cpus/--memory cap -- all four are lightweight
# orchestration (HTTP calls, git, occasional rsync); the actual heavy CPU/
# RAM burn during an execution run happens in Ollama (inference) and
# code-server (the `docker exec`'d build/test commands), neither of which
# this repo manages, so cap those too via Unraid's own container settings
# ("Extra Parameters" takes the same --cpus/--memory flags). The point of
# capping even these lightweight containers isn't that they need much --
# it's that a hard --memory limit means a runaway/bugged process gets
# OOM-killed inside its own container by the kernel, not left free to
# start eating host RAM until something more important gets picked instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE="${IMAGE:-docker.io/brianjwalz/tududi-bridge:latest}"
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

echo "== push =="
# Requires `docker login` already done for this account. Pushed so Unraid's
# Force Update on tududi-planner/tududi-executor (which pull this same tag
# via their template's Repository field) picks up this exact build --
# without this, Force Update has nothing new to find and silently keeps
# running whatever image was there before.
docker push "$IMAGE"

echo "== restart containers =="
docker rm -f tududi-ingest tududi-worker tududi-planner tududi-executor >/dev/null 2>&1 || true

docker run -d \
  --name tududi-ingest \
  --restart unless-stopped \
  --cpus="1.0" --memory="512m" \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  "$IMAGE" python ingest.py

docker run -d \
  --name tududi-worker \
  --restart unless-stopped \
  --cpus="1.0" --memory="1g" \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  -v "$SCRIPT_DIR/prompts:/app/prompts:ro" \
  "$IMAGE" python worker.py

docker run -d \
  --name tududi-planner \
  --restart unless-stopped \
  --cpus="1.0" --memory="1g" \
  -e TZ=America/Chicago \
  "${ENV_FILE_ARGS[@]}" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR:/data" \
  -v "$SCRIPT_DIR/prompts:/app/prompts:ro" \
  "$IMAGE" python planner.py

docker run -d \
  --name tududi-executor \
  --restart unless-stopped \
  --cpus="2.0" --memory="2g" \
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

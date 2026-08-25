# tududi-bridge

Dump a half-formed thought into ntfy from your phone. A local Ollama model turns
it into a properly written task on the right tududi project a minute or two
later. Tag a task `plan-me` and a third daemon hands it to headless Claude
Code for full scoping, grounded in the actual repo — see
[AI planning](#ai-planning-tag-triggered) below.

Nothing inbound is exposed. The bridge holds an outbound connection to ntfy and
talks to tududi and Ollama over the LAN.

```
phone ──publish──> ntfy.sh ──outbound stream──> ingest ──> SQLite queue
                                                   │              │
                                                   └─> stub task  │
                                                                  ▼
                                        tududi <── worker <── Ollama (3 passes)
```

## Why it's built this way

**Ingest never blocks on inference.** A captured thought is durable in SQLite
and visible in tududi within a second, as a stub tagged `triage:pending`. The
worker enriches that same task in place. Ollama can be down for a day and you
lose polish, not thoughts.

**One item at a time.** On a dual-channel DDR4 box, token generation is bound by
memory bandwidth. Two concurrent inferences finish slower in aggregate than two
sequential ones.

**Three passes, not one.** Classify → draft → critique. The third pass exists
specifically to catch the failure mode that makes small models useless for this:
inventing a confident root cause or a plausible-looking file path that isn't
real. It gets the draft and the raw text and strips anything unsupported.

**The raw capture is always preserved verbatim** at the bottom of every task
description. The model can never silently mangle what you meant.

## Setup

This assumes ntfy, tududi, and Ollama are all already reachable on your LAN as
separate hosts (not sharing a Docker network with the bridge) — set their
addresses under `ntfy.base_url`, `tududi.base_url`, and `ollama.base_url` in
`config.yml`. If you instead run any of them as a container on the same
Docker host as the bridge, you can address that one by container name over a
shared network, but nothing in this repo requires it.

**1. Directories and config.**

```bash
mkdir -p /mnt/user/appdata/tududi-bridge/{config,data}
cp config.example.yml /mnt/user/appdata/tududi-bridge/config/config.yml
```

**2. API token.** tududi → Settings → API Tokens → create one (`tt_...`). Put it
in a `.env` next to `run.sh` as `TUDUDI_API_TOKEN=tt_...`.

**3. Pull the model** on your existing Ollama instance. Locally, if you have
shell access to that host:

```bash
ollama pull qwen3:30b-a3b
```

Or remotely, against its API, from anywhere that can reach it:

```bash
curl http://<ollama-host>:11434/api/pull -d '{"name": "qwen3:30b-a3b"}'
```

**4. Discover project IDs.** This also checks every connection. Build the image
first if you haven't yet (`docker build -t tududi-bridge:latest .`), then:

```bash
docker run --rm \
  -v /mnt/user/appdata/tududi-bridge/config:/config:ro \
  -v /mnt/user/appdata/tududi-bridge/data:/data \
  --env-file .env \
  tududi-bridge:latest python discover.py
```

Paste the printed `topics:` block into your config, replace each `CHANGEME`
with a random suffix, and set the notes.

**5. Go.**

```bash
./run.sh
docker logs -f tududi-worker
```

`run.sh` builds the image and (re)starts the `tududi-ingest` and
`tududi-worker` containers with plain `docker build`/`docker run` — no
docker-compose. Host paths default to `/mnt/user/appdata/tududi-bridge`;
override by exporting `APPDATA_DIR` before running it. Re-run `./run.sh`
any time you change the Dockerfile or `requirements.txt`; for prompt-only
edits, `docker restart tududi-worker` is enough since `prompts/` is bind-mounted.

**6. Phone.** Install ntfy from Play Store or F-Droid, subscribe to each topic.
The message bar at the bottom of a topic view publishes directly. The Android
share sheet also publishes to a topic — that's the fastest capture path for
links and selected text from any app.

**7. (Optional) AI planning.** A third daemon, `tududi-planner`, hands
`plan-me`-tagged tasks to headless Claude Code for full scoping — see
[AI planning](#ai-planning-tag-triggered) below for what it does. To enable it:

```bash
# One-time, on a machine with an active Claude subscription (Pro/Max/Team):
claude setup-token
```

Put the printed token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. Do **not**
also put `ANTHROPIC_API_KEY` in `.env` or export it anywhere this container's
environment is built from — the planner refuses to start if it sees one,
since Claude Code would otherwise silently prefer it over the subscription
token and switch billing to pay-per-token. Add `github.repos` and
`ntfy.reply_topic` to `config.yml` (see `config.example.yml`), then
`./run.sh` as usual — it now also builds and starts `tududi-planner`.
Managing containers through Unraid's Docker UI instead of `run.sh`? See
`unraid-template.xml`.

## Topic conventions

The topic picks the project. Two more free routing signals come from ntfy's
publish dialog:

- **Tags** → `bug`, `feature`, `chore` are read as classification hints and
  override the model's inference
- **Priority** → carried through to the task priority

So a max-priority message tagged `bug` on `manabase-lgs-xsa18m` is fully routed
before the model sees it. The model's remaining job is just writing it up well.

> **On ntfy.sh, a topic name is a password.** Anyone who guesses it can read
> everything you capture and inject tasks into your projects. Use long random
> suffixes, or self-host ntfy on a VPS with auth, or use ntfy Pro reserved
> topics. Do not use guessable names like `bugs` or `brian-tasks`.

Also note ntfy.sh caches undelivered messages for roughly 12 hours. The cursor
in `meta` means a restart replays from the last seen message, but an ingest
outage longer than the cache window loses whatever arrived during it.

## Tuning the model

| | RAM @ Q4_K_M | rough tok/s on a 5700G |
|---|---|---|
| `qwen3:8b` | ~5 GB | 6–8 |
| `qwen3:14b` | ~9 GB | 3–4 |
| `qwen3:30b-a3b` | ~18 GB | 10–15 |

The MoE is both faster and more capable than the 14B dense model, because only
~3B parameters are active per token. Check `ollama list` against what's current
before committing — this part of the ecosystem moves quickly.

**Watch RAM.** 18 GB resident with `keep_alive: -1` on a 32 GB box running Plex,
your other containers, and cache is aggressive. If you see the mover stalling or
containers OOMing, drop to `qwen3:14b` — a full three-pass run at 3 tok/s is
still well under three minutes, which is inside your tolerance.

`num_thread: 8` matches physical cores. Going to 16 typically makes llama.cpp
slower, not faster.

## Tuning the prompts

`prompts/` is bind-mounted read-only into the worker, so edits take effect on
`docker restart tududi-worker` — no rebuild. Each file is Markdown with a YAML
frontmatter block holding its JSON schema and sampling settings.

The single highest-leverage change you can make is replacing the worked examples
in `01_classify.md` and `02_draft.md` with three or four of your own real
captures and the tasks you'd have written by hand. Few-shot examples in your own
voice buy more consistency than a bigger model does.

Because `seed` is fixed and temperature is low, re-running the same capture
gives the same output — so you can iterate on a prompt and actually see whether
your change helped.

## AI planning (tag-triggered)

Tag any tududi task `plan-me` and the `tududi-planner` daemon picks it up: it
hands the task to headless Claude Code for full scoping — not quick triage,
a real implementation plan — optionally grounded in the project's actual
GitHub repo, and writes the result back into the task's note.

```
tududi (tag: plan-me) ──poll──> planner ──claude -p──> scoped plan ──> tududi.note
                                    │
                                    ├─> git clone (if github.repos maps the project)
                                    └─> ntfy question <──> ntfy reply (if blocked)
```

**Billed against your Claude subscription, not the Anthropic API.** The
planner shells out to the `claude` CLI in headless mode (`claude -p`) using
the long-lived OAuth token from `claude setup-token` (Setup step 7), not an
API key. It refuses to start if `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` is
set anywhere in its environment, since Claude Code would otherwise silently
prefer that over the subscription token.

**Clarifying questions are async.** If Claude genuinely can't produce a full
plan without more information, it asks — capped at 3 questions, and only
when truly blocking. The task gets tagged `plan:awaiting-input`, a question
is published to `ntfy.reply_topic`, and the planner moves on to the next
queued task rather than blocking on an answer. Reply on that topic and the
task resumes automatically on the next poll. If more than one task is
`awaiting-input` at once, prefix your reply with the `PLAN-XXXXXX` token from
the question; with only one open question, a bare reply resolves it.

**GitHub grounding is a local clone, not a REST API.** Map a tududi project
to a repo under `github.repos: {project_id: "owner/repo"}` in `config.yml`
and the planner shallow-clones it fresh before each planning pass, giving
Claude its own Read/Grep/Glob tools against the real code — no Bash, no
Edit, no Write, and no prompting (`--permission-mode dontAsk`). Projects
without a mapping just plan from the task's note and `PROJECT_NOTES` alone.

Tasks land tagged `plan:done` (or `plan:awaiting-input` while parked,
`plan:plan-failed` after exhausting retries) in place of the trigger tag —
the planner only ever touches tags it owns; everything else on the task
(including tags the triage pipeline set) is left alone.

## Operating it

```bash
# queue state
docker exec tududi-worker python -c \
  "import config,db;print(db.stats(db.connect(config.DB_PATH)))"

# what the model actually did, most recent first
sqlite3 /mnt/user/appdata/tududi-bridge/data/queue.db \
  "SELECT id,topic,status,attempts,substr(raw_text,1,50) FROM queue ORDER BY id DESC LIMIT 20;"

# retry everything that failed
sqlite3 /mnt/user/appdata/tududi-bridge/data/queue.db \
  "UPDATE queue SET status='pending',attempts=0,next_attempt_at=0 WHERE status='failed';"

# planner queue state (separate table, same DB file)
docker exec tududi-planner python -c \
  "import config,db;print(db.plan_stats(db.connect(config.DB_PATH)))"

# what's currently awaiting a reply, and the question that was asked
sqlite3 /mnt/user/appdata/tududi-bridge/data/queue.db \
  "SELECT id,tududi_task_id,correlation_token,question_text FROM plan_queue WHERE status='awaiting_input';"

# what a completed plan actually cost, most recent first
sqlite3 /mnt/user/appdata/tududi-bridge/data/queue.db \
  "SELECT id,tududi_task_id,json_extract(result_json,'$.cost_usd') AS cost_usd FROM plan_queue WHERE status='done' ORDER BY id DESC LIMIT 20;"
```

Every completed row stores a `result_json` containing the classification, the
issues the critique pass found, and the wall-clock seconds. That's your data for
deciding whether a prompt change was an improvement.

Tasks land tagged `auto-triaged` plus `type:*`, `size:*`, and either `ready` or
`needs-refinement`. Point Claude Code at `ready`.

Titles are standardized as `Type: description` (e.g. `Bug: ...`, `Feature: ...`),
built from the classification pass plus the drafted/critiqued summary. The
description body is Markdown — a `## Summary` plus whichever of `## Acceptance
criteria`, `## Open questions`, and `## Likely files` apply — with the verbatim
capture always preserved at the bottom under `## Original capture`.

## Verify before you trust it

tududi's endpoint shapes have moved between releases. If `discover.py` reports a
404, open the Swagger UI at `<your-tududi>/api/v1`, check the actual paths and
the create-task request body, and override them under `tududi.paths` in
`config.yml`. The client reads every path from config precisely so you never
have to edit Python for this.

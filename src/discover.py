"""Connectivity check + project ID discovery.

    docker run --rm -v /mnt/user/appdata/tududi-bridge/config:/config:ro \
      -v /mnt/user/appdata/tududi-bridge/data:/data --env-file .env \
      tududi-bridge:latest python discover.py

Prints a ready-to-paste `topics:` block for config.yml.
"""
import sys

import config
import db
import sandbox
from ollama import Ollama
from tududi import Tududi, TududiError


def main():
    cfg = config.load()
    ok = True

    print("== tududi ==")
    td = Tududi(cfg)
    try:
        projects = td.list_projects()
        print(f"  connected: {cfg.tududi_base} ({len(projects)} projects)\n")
        print("  suggested config.yml block:\n")
        print("topics:")
        for p in projects:
            pid = p.get("id") or p.get("uid")
            name = (p.get("name") or "").lower().replace(" ", "-")
            slug = "".join(ch for ch in name if ch.isalnum() or ch == "-")
            print(f"  {slug}-CHANGEME:")
            print(f"    project_id: {pid}   # {p.get('name')}")
            print(f"    notes: \"\"")
        print()
    except TududiError as e:
        ok = False
        print(f"  FAILED: {e}")
        print("  -> check TUDUDI_API_TOKEN and the paths under tududi.paths\n")

    print("== ollama ==")
    llm = Ollama(cfg)
    try:
        import requests
        r = requests.get(f"{cfg.ollama_base}/api/tags", timeout=10)
        names = [m["name"] for m in r.json().get("models", [])]
        print(f"  connected: {cfg.ollama_base}")
        if cfg.model in names:
            print(f"  model present: {cfg.model}")
        else:
            ok = False
            print(f"  MODEL MISSING: {cfg.model}")
            print(f"  available: {', '.join(names) or '(none)'}")
            print(f"  -> ollama pull {cfg.model}  (run on the Ollama host, "
                  f"or POST {{\"name\": \"{cfg.model}\"}} to {cfg.ollama_base}/api/pull)")
    except Exception as e:
        ok = False
        print(f"  FAILED: {e}")
    print()

    print("== queue ==")
    conn = db.connect(config.DB_PATH)
    print(f"  {config.DB_PATH}: {db.stats(conn) or 'empty'}")
    print()

    print("== topics configured ==")
    for t in cfg.topic_list:
        pid, notes = cfg.project_for(t)
        print(f"  {t} -> project {pid}")
    if not cfg.topic_list:
        ok = False
        print("  none -- nothing will be ingested")
    print()

    print("== executor ==")
    backend_ok, detail = sandbox.preflight(cfg)
    print(f"  backend ({cfg.exec_backend}): {detail}")
    if not backend_ok:
        ok = False

    try:
        import requests
        r = requests.get(f"{cfg.ollama_base}/api/tags", timeout=10)
        names = [m["name"] for m in r.json().get("models", [])]
        if cfg.exec_model in names:
            print(f"  exec model present: {cfg.exec_model}")
        else:
            ok = False
            print(f"  EXEC MODEL MISSING: {cfg.exec_model}")
            print(f"  available: {', '.join(names) or '(none)'}")
    except Exception as e:
        ok = False
        print(f"  FAILED checking exec model: {e}")

    try:
        cfg.workspace_root.mkdir(parents=True, exist_ok=True)
        probe = cfg.workspace_root / ".bridge_write_test"
        probe.write_text("ok")
        probe.unlink()
        print(f"  workspace_root writable: {cfg.workspace_root}")
    except OSError as e:
        ok = False
        print(f"  workspace_root NOT writable: {cfg.workspace_root}: {e}")

    print(f"  exec_queue: {db.exec_stats(conn) or 'empty'}")

    # Informational only -- never flips `ok`. The Mac being offline is an
    # expected, handled fallback for mac.projects, not misconfiguration.
    if cfg.mac_enabled:
        print(f"  mac backend: {sandbox.mac_status(cfg)}")
        if cfg.mac_projects:
            print(f"  mac-routed projects: {', '.join(sorted(cfg.mac_projects))}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

"""Minimal tududi REST client.

NOTE: tududi exposes versioned Swagger docs at /api/v1. Endpoint shapes have
moved between releases -- if a call 404s, check the Swagger UI on your instance
and override the paths under `tududi.paths` in config.yml rather than editing
this file.
"""
import requests


class TududiError(RuntimeError):
    pass


def _tag_names(tags):
    """Read endpoints (list_tasks/get_task) return tags as full objects
    (e.g. {"id": 5, "name": "plan-me"}), not the plain strings create_task/
    update_task expect in a request body -- normalize to plain strings so
    every caller gets one consistent shape regardless of which endpoint it
    came from."""
    out = []
    for t in (tags or []):
        if isinstance(t, dict):
            out.append(t.get("name") or t.get("title") or str(t))
        else:
            out.append(str(t))
    return out


class Tududi:
    def __init__(self, cfg):
        self.base = cfg.tududi_base
        self.paths = cfg.tududi_paths
        self.tag_query_param = cfg.tag_query_param
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {cfg.tududi_token}",
            "Content-Type": "application/json",
        })

    def _call(self, method, path, **kw):
        url = self.base + path
        try:
            r = self.s.request(method, url, timeout=30, **kw)
        except requests.RequestException as e:
            raise TududiError(f"{method} {url}: {e}") from e
        if r.status_code >= 400:
            raise TududiError(f"{method} {url} -> {r.status_code}: {r.text[:400]}")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def list_projects(self):
        data = self._call("GET", self.paths["list_projects"])
        # Instances return either a bare list or {"projects": [...]}
        if isinstance(data, dict):
            return data.get("projects", data.get("data", []))
        return data

    def create_task(self, *, name, project_id=None, note=None,
                    priority=None, tags=None, parent_task_id=None):
        """`parent_task_id` takes the numeric `id` a GET/create response
        returns (NOT the `uid` string used everywhere else) -- confirmed
        live against a real instance: POST with parent_task_id set creates
        a real subtask, additively, without touching the parent's existing
        subtasks (unlike PATCHing a `subtasks` array onto the parent, which
        replaces the whole collection -- don't use that shape)."""
        body = {"name": name[:255]}
        if project_id is not None:
            body["project_id"] = project_id
        if note:
            body["note"] = note
        if priority:
            body["priority"] = priority
        if tags:
            body["tags"] = tags
        if parent_task_id is not None:
            body["parent_task_id"] = parent_task_id
        data = self._call("POST", self.paths["create_task"], json=body)
        task = data.get("task", data) if isinstance(data, dict) else {}
        task_id = task.get("uid") or task.get("uuid") or task.get("id")
        if task_id is None:
            raise TududiError(f"no task id in create response: {str(data)[:300]}")
        return str(task_id)

    def update_task(self, task_id, **fields):
        path = self.paths["update_task"].format(id=task_id)
        return self._call("PATCH", path, json=fields)

    def list_tasks(self, *, tag=None):
        params = {}
        if tag:
            params[self.tag_query_param] = tag
        data = self._call("GET", self.paths["list_tasks"], params=params)
        # Same defensive shape handling as list_projects().
        tasks = data.get("tasks", data.get("data", [])) if isinstance(data, dict) else data
        for t in tasks:
            if isinstance(t, dict):
                t["tags"] = _tag_names(t.get("tags"))
        return tasks

    def get_task(self, task_id):
        path = self.paths["get_task"].format(id=task_id)
        data = self._call("GET", path)
        task = data.get("task", data) if isinstance(data, dict) else {}
        task["tags"] = _tag_names(task.get("tags"))
        return task

    def ping(self):
        self.list_projects()
        return True

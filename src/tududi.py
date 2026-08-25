"""Minimal tududi REST client.

NOTE: tududi exposes versioned Swagger docs at /api/v1. Endpoint shapes have
moved between releases -- if a call 404s, check the Swagger UI on your instance
and override the paths under `tududi.paths` in config.yml rather than editing
this file.
"""
import requests


class TududiError(RuntimeError):
    pass


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
                    priority=None, tags=None):
        body = {"name": name[:255]}
        if project_id is not None:
            body["project_id"] = project_id
        if note:
            body["note"] = note
        if priority:
            body["priority"] = priority
        if tags:
            body["tags"] = tags
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
        if isinstance(data, dict):
            return data.get("tasks", data.get("data", []))
        return data

    def get_task(self, task_id):
        path = self.paths["get_task"].format(id=task_id)
        data = self._call("GET", path)
        return data.get("task", data) if isinstance(data, dict) else {}

    def ping(self):
        self.list_projects()
        return True

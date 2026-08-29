"""Ollama chat client using schema-constrained structured output.

Passing a JSON Schema as `format` makes the sampler emit only tokens that keep
the output valid against the schema. That is what makes a 3B-active MoE usable
as a pipeline component instead of a chatbot.
"""
import json

import requests


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, cfg, *, model=None, num_ctx=None, keep_alive=None):
        """`model`/`num_ctx`/`keep_alive` let a caller (executor.py) build a
        client pinned to cfg.exec_model/cfg.exec_num_ctx/cfg.exec_keep_alive
        instead of the triage-sized cfg.model/cfg.num_ctx/cfg.keep_alive --
        so chat_json() picks up the right settings too via self.model/
        self.base_options/self.keep_alive, without chat_json() itself
        needing to change at all."""
        self.base = cfg.ollama_base
        self.model = model or cfg.model
        self.timeout = cfg.request_timeout
        self.keep_alive = keep_alive if keep_alive is not None else cfg.keep_alive
        self.base_options = {
            "num_ctx": num_ctx or cfg.num_ctx,
            "num_thread": cfg.num_thread,
            "seed": cfg.seed,
            "top_p": 0.9,
            "repeat_penalty": 1.05,
        }

    def chat_json(self, system: str, user: str, schema: dict,
                  temperature: float = 0.1, num_predict: int = 800) -> dict:
        options = dict(self.base_options)
        options["temperature"] = temperature
        options["num_predict"] = num_predict

        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": options,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            r = requests.post(f"{self.base}/api/chat", json=payload,
                              timeout=self.timeout)
        except requests.RequestException as e:
            raise OllamaError(f"ollama unreachable: {e}") from e
        if r.status_code >= 400:
            raise OllamaError(f"ollama {r.status_code}: {r.text[:400]}")

        content = r.json().get("message", {}).get("content", "").strip()
        if not content:
            raise OllamaError("empty completion")
        # Schema-constrained output should already be clean, but thinking-capable
        # models occasionally wrap it. Be forgiving.
        if content.startswith("```"):
            content = content.strip("`")
            content = content.split("\n", 1)[-1] if content.startswith("json") else content
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise OllamaError(f"unparseable JSON: {e}: {content[:300]}") from e

    def chat_tools(self, messages: list, tools: list, *, model=None, num_ctx=None,
                   temperature=None, num_predict=None) -> dict:
        """Tool-calling turn for agent.py's loop. Returns the raw response
        `message` dict (content + tool_calls) rather than parsed JSON --
        unlike chat_json(), it's the caller's job to decide whether a turn
        produced a tool call, plain content, or both, and drive the next
        turn accordingly.

        chat_json() is deliberately left untouched (worker.py/pipeline.py
        depend on its exact behaviour) -- this is a separate method, not a
        new mode bolted onto it.
        """
        options = dict(self.base_options)
        if num_ctx is not None:
            options["num_ctx"] = num_ctx
        if temperature is not None:
            options["temperature"] = temperature
        if num_predict is not None:
            options["num_predict"] = num_predict

        payload = {
            "model": model or self.model,
            "stream": False,
            "tools": tools,
            "keep_alive": self.keep_alive,
            "options": options,
            "messages": messages,
        }
        try:
            r = requests.post(f"{self.base}/api/chat", json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise OllamaError(f"ollama unreachable: {e}") from e
        if r.status_code >= 400:
            raise OllamaError(f"ollama {r.status_code}: {r.text[:400]}")

        message = r.json().get("message")
        if not message:
            raise OllamaError("empty message in response")
        return message

    def warm(self):
        """Load the model into RAM so the first real request isn't a cold start."""
        requests.post(
            f"{self.base}/api/chat",
            json={"model": self.model, "messages": [], "keep_alive": self.keep_alive},
            timeout=self.timeout,
        )

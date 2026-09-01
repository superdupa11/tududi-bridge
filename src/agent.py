"""Bounded tool-calling agent loop for executor.py.

Drives Ollama's tool-calling endpoint (ollama.Ollama.chat_tools) turn by
turn: the model calls a tool, the loop executes it against the workspace
through sandbox.py, appends the result, and repeats until the model calls
finish(), or cfg.executor_max_steps / cfg.executor_total_timeout is hit.

Three things can also PAUSE the loop before either of those: the model
calling `ask_question`, sandbox.py flagging a `git push`/`docker build`/
`docker push` as needing human approval, or the loop itself noticing a `run`
command has failed the exact same way REPEAT_FAILURE_PARK_AT times in a row
with nothing changed in between and synthesizing an ask_question-shaped
park instead of letting a small exec model grind through max_steps
repeating a command that was never going to succeed (see REPEAT_FAILURE_*
below). Either way `run()` returns a "parked" outcome with the live
conversation state instead of a finish() report -- executor.py persists
that, notifies the human on the run's dedicated ntfy topic, and calls back
into `run()` with `resume_messages` (the reply already injected) once one
arrives. `elapsed_so_far`/`steps_so_far` let the step/time budgets span an
arbitrarily long pause without resetting or double-counting active work.

A turn with no tool_calls is first checked for a well-formed call the model
wrote as plain message content instead (typically markdown-fenced JSON) --
see _parse_content_tool_call -- and dispatched for real if found, rather
than silently discarded (observed live: a dropped-but-correct write_file
call led the model to confidently report a file as created that was never
actually written). Only once two turns in a row have neither a real
tool_calls entry nor a recoverable one does the loop give up on native
tool calling and switch to a schema-constrained single-action JSON loop
over the existing ollama.chat_json(), so the feature doesn't hard-depend on
tool-call support. That fallback path is inherently a rougher approximation
(one action per turn, no native tool-result role, and no ask_question/
approval parking -- see run()'s docstring).
"""
import json
import logging
import os
import time

from ollama import OllamaError
import sandbox

log = logging.getLogger("agent")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at a path inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Path relative to, or inside, the workspace root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file, optionally a line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "1-indexed starting line, default 1"},
                    "limit": {"type": "integer", "description": "Max lines to read, default 2000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact, unique occurrence of old_string with new_string "
                           "in an existing file. Errors if old_string isn't found, or isn't unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": "Run a shell command in the workspace and return its exit code, "
                           "stdout, and stderr. A `git push`, `docker build`, or `docker push` "
                           "pauses the run for human approval instead of executing immediately "
                           "-- you'll get the outcome as the result of this same call once "
                           "they respond.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_question",
            "description": "Pause the run and ask the human a genuine, blocking question -- "
                           "something you can't reasonably decide yourself and that would "
                           "materially change what you do next. You'll get their answer as "
                           "the result of this same call once they respond. Don't use this "
                           "for ordinary judgment calls; make those yourself and note the "
                           "assumption in your eventual finish() summary instead.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_update",
            "description": "Push a short status update to the human, without pausing the run "
                           "-- e.g. progress notes, test output, or a file you already produced "
                           "(a screenshot, a coverage report) that's worth them seeing now "
                           "rather than only at the end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "attach_path": {"type": "string",
                                    "description": "Optional path (inside the workspace) to a "
                                                   "file to attach, e.g. a screenshot or log."},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call exactly once, when the plan is fully implemented or you are "
                           "stuck, to end the run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "files_changed": {"type": "array", "items": {"type": "string"}},
                    "commands_run": {"type": "array", "items": {"type": "string"}},
                    "acceptance_check": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "criterion": {"type": "string"},
                                "status": {"type": "string",
                                          "enum": ["met", "not_met", "unverified"]},
                                "note": {"type": "string"},
                            },
                            "required": ["criterion", "status"],
                        },
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["summary", "acceptance_check", "confidence"],
            },
        },
    },
]

FALLBACK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tool": {"type": "string",
                 "enum": ["list_dir", "read_file", "write_file", "edit_file", "run", "finish"]},
        "args": {"type": "object"},
    },
    "required": ["tool", "args"],
}

NO_TOOL_CALL_NUDGE = ("Call one of the available tools, or call finish() if the work "
                     "is complete.")

# A model re-issuing the exact same `run` command right after it just failed
# the identical way means it isn't adapting to the error, not that a retry
# might succeed (observed live: a small exec model retried `pip install ...`
# verbatim three times in a row after `pip: command not found`, burning
# steps instead of noticing pip wasn't on PATH). Nudge once; if it repeats
# anyway, park the run the same way ask_question does rather than let it
# grind through max_steps silently -- a human can say "use pip3" or "that's
# expected, skip it" in seconds.
REPEAT_FAILURE_NUDGE_AT = 2
REPEAT_FAILURE_PARK_AT = 3
REPEAT_FAILURE_NUDGE = (
    "That exact command just failed the same way it did last time -- repeating it "
    "verbatim won't produce a different result. Either adapt (a different command, "
    "installing or locating the missing tool, working around it another way) or, if "
    "this is a genuine environment limitation you can't work around, stop retrying "
    "and call finish() with confidence: low explaining what's blocked, per the Bias "
    "section."
)

_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def _parse_content_tool_call(content):
    """A model sometimes writes a perfectly well-formed tool call as plain
    message content (typically markdown-fenced JSON) instead of using
    tool_calls -- silently discarding that (as the no-tool_calls path used
    to) means nothing executes while the model believes it already acted,
    which can cascade into a confidently fabricated finish() report on a
    change that never actually landed (observed live: a full, correct
    write_file call written this way was dropped, and the model went on to
    report the file as created). Same fenced-JSON stripping ollama.chat_json
    already uses. Returns (name, args) if `content` parses as
    {"name": <known tool>, "arguments": {...}}, else None."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if text.startswith("json") else text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name")
    if name not in _TOOL_NAMES:
        return None
    return name, _parse_args(parsed.get("arguments"))


class AgentError(RuntimeError):
    pass


class _NeedsApproval(RuntimeError):
    def __init__(self, command, reason):
        super().__init__(reason)
        self.command = command
        self.reason = reason


def _resolve(workspace_root: str, path: str) -> str:
    """Resolves `path` (relative or absolute) against workspace_root and
    rejects anything outside it. realpath() follows symlinks before the
    containment check runs, so this blocks both ../ traversal and a symlink
    planted inside the workspace pointing back out of it."""
    root = os.path.realpath(workspace_root)
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(candidate)
    if real != root and not real.startswith(root + os.sep):
        raise AgentError(f"path {path!r} resolves outside the workspace root")
    return real


def _tool_list_dir(workspace_root, args):
    real = _resolve(workspace_root, args["path"])
    if not os.path.isdir(real):
        return f"error: not a directory: {args['path']}"
    entries = sorted(os.listdir(real))
    return "\n".join(entries) if entries else "(empty)"


def _tool_read_file(workspace_root, args):
    real = _resolve(workspace_root, args["path"])
    if not os.path.isfile(real):
        return f"error: not a file: {args['path']}"
    offset = max(int(args.get("offset") or 1), 1)
    limit = int(args.get("limit") or 2000)
    with open(real, "r", errors="replace") as fh:
        lines = fh.readlines()
    sliced = lines[offset - 1:offset - 1 + limit]
    return "".join(f"{i}\t{line}" for i, line in enumerate(sliced, offset))


def _tool_write_file(workspace_root, args):
    real = _resolve(workspace_root, args["path"])
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w") as fh:
        fh.write(args["content"])
    return f"wrote {len(args['content'])} bytes to {args['path']}"


def _tool_edit_file(workspace_root, args):
    real = _resolve(workspace_root, args["path"])
    if not os.path.isfile(real):
        return f"error: not a file: {args['path']}"
    with open(real, "r") as fh:
        content = fh.read()
    old, new = args["old_string"], args["new_string"]
    count = content.count(old)
    if count == 0:
        return f"error: old_string not found in {args['path']}"
    if count > 1:
        return f"error: old_string is not unique in {args['path']} ({count} occurrences)"
    with open(real, "w") as fh:
        fh.write(content.replace(old, new, 1))
    return f"edited {args['path']}"


def _tool_run(cfg, workspace_root, backend, args):
    code, out, err = sandbox.run(cfg, args["command"], cwd=workspace_root,
                                 timeout=cfg.executor_step_timeout, backend=backend)
    if code == sandbox.NEEDS_APPROVAL:
        raise _NeedsApproval(command=out, reason=err)
    return f"exit={code}\nstdout:\n{out}\nstderr:\n{err}"


def _tool_send_update(ntfy_publish, workspace_root, args):
    attach_path = args.get("attach_path")
    real_attach = None
    if attach_path:
        try:
            real_attach = _resolve(workspace_root, attach_path)
        except AgentError as e:
            return f"error: {e}"
        if not os.path.isfile(real_attach):
            return f"error: not a file: {attach_path}"
    try:
        ntfy_publish(args.get("message") or "", real_attach)
    except Exception as e:  # noqa: BLE001 -- any publish failure is reported, never fatal to the run
        return f"error sending update: {e}"
    return "sent"


def _report_progress(ntfy_publish, name, result):
    """File-changing tools (write_file/edit_file/run) get an unsolicited
    ntfy ping so a human watching the run's topic sees activity in
    real time, not just the model's own voluntary send_update calls.
    list_dir/read_file stay silent -- exploration is high-volume and
    low-signal compared to actions that actually touch the workspace."""
    try:
        ntfy_publish(f"[{name}] {result[:300]}", None)
    except Exception:  # noqa: BLE001 -- a progress ping must never break the loop
        pass


def _dispatch(cfg, workspace_root, backend, ntfy_publish, name, args):
    try:
        if name == "list_dir":
            return _tool_list_dir(workspace_root, args)
        if name == "read_file":
            return _tool_read_file(workspace_root, args)
        if name == "write_file":
            result = _tool_write_file(workspace_root, args)
            _report_progress(ntfy_publish, name, result)
            return result
        if name == "edit_file":
            result = _tool_edit_file(workspace_root, args)
            _report_progress(ntfy_publish, name, result)
            return result
        if name == "run":
            result = _tool_run(cfg, workspace_root, backend, args)
            _report_progress(ntfy_publish, name, result)
            return result
        if name == "send_update":
            return _tool_send_update(ntfy_publish, workspace_root, args)
    except (AgentError, OSError) as e:
        return f"error: {e}"
    return f"error: unknown tool {name!r}"


def _parse_args(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw or {}


def _run_exit_code(result: str):
    """Pulls the exit code back out of _tool_run's `exit=<code>\n...` result
    string -- avoids widening _dispatch's return contract (a single string,
    the same shape for every tool) just for one tool's internal detail."""
    if not result.startswith("exit="):
        return None
    head = result.split("\n", 1)[0]
    try:
        return int(head[len("exit="):])
    except ValueError:
        return None


def _handle_tool_call(cfg, workspace_root, backend, ntfy_publish, messages, transcript,
                      name, args, steps_so_far, step, elapsed_fn, run_state):
    """Executes one resolved tool call (finish/ask_question/everything else
    via _dispatch), appending to messages/transcript as it goes. Returns an
    outcome dict if run()'s loop should return immediately (finish,
    ask_question, an approval-needing command, or a `run` command that's now
    failed identically REPEAT_FAILURE_PARK_AT times in a row), or None if it
    should continue to the next turn. Shared by the native tool_calls loop
    and _parse_content_tool_call's fallback path below, so a call dispatched
    either way behaves identically.

    `run_state` is a mutable {"command", "streak"} dict, owned by run()'s
    caller and threaded through every call this loop makes, that tracks
    consecutive identical-command `run` failures across turns -- see the
    REPEAT_FAILURE_* constants above."""
    if name == "finish":
        transcript.append({"role": "system", "note": "finish() called"})
        return {"status": "done", "report": args, "elapsed_seconds": elapsed_fn(),
               "steps_used": steps_so_far + step}

    if name == "ask_question":
        question = args.get("question") or "The agent has a question."
        transcript.append({"role": "system", "note": f"ask_question: {question}"})
        return {"status": "parked", "kind": "question", "question": question,
               "pending_command": None, "resume_messages": messages,
               "elapsed_seconds": elapsed_fn(), "steps_used": steps_so_far + step}

    try:
        result = _dispatch(cfg, workspace_root, backend, ntfy_publish, name, args)
    except _NeedsApproval as e:
        transcript.append({"role": "system", "note": f"approval needed: {e.command}"})
        return {"status": "parked", "kind": "approval", "question": e.reason,
               "pending_command": e.command, "resume_messages": messages,
               "elapsed_seconds": elapsed_fn(), "steps_used": steps_so_far + step}

    tool_msg = {"role": "tool", "name": name, "content": result}
    messages.append(tool_msg)
    transcript.append(tool_msg)

    if name == "run":
        command = args.get("command")
        failed = _run_exit_code(result) not in (None, 0)
        if failed and command == run_state["command"]:
            run_state["streak"] += 1
        else:
            run_state["streak"] = 1 if failed else 0
        run_state["command"] = command

        if run_state["streak"] >= REPEAT_FAILURE_PARK_AT:
            question = (f"`{command}` has now failed the same way "
                       f"{run_state['streak']} times in a row with nothing changed "
                       "in between -- looks like an environment limitation, not "
                       "something a retry will fix. How should I proceed?")
            transcript.append({"role": "system",
                               "note": f"parked: repeated identical run() failure -- {question}"})
            return {"status": "parked", "kind": "question", "question": question,
                   "pending_command": None, "resume_messages": messages,
                   "elapsed_seconds": elapsed_fn(), "steps_used": steps_so_far + step}
        if run_state["streak"] == REPEAT_FAILURE_NUDGE_AT:
            nudge = {"role": "user", "content": REPEAT_FAILURE_NUDGE}
            messages.append(nudge)
            transcript.append(nudge)

    return None


TIMED_OUT_REPORT = {
    "summary": "step or time budget exhausted before finish() was called",
    "files_changed": [], "commands_run": [], "acceptance_check": [], "confidence": "low",
}

STOPPED_REPORT = {
    "summary": "stopped by user request before finish() was called",
    "files_changed": [], "commands_run": [], "acceptance_check": [], "confidence": "low",
}


def run(cfg, llm, system_text, task_prompt, workspace_root, ntfy_publish, *,
       max_steps=None, resume_messages=None, elapsed_so_far=0.0, steps_so_far=0, backend=None,
       stop_check=None):
    """Runs the bounded tool loop. Returns (outcome, transcript):

      outcome    -- a dict with a "status" key:
                     "done"      -- {"status": "done", "report": {...finish() args...}}
                     "timed_out" -- {"status": "timed_out", "report": {...}}
                     "stopped"   -- {"status": "stopped", "report": {...}}
                     "parked"    -- {"status": "parked", "kind": "question"|"approval",
                                     "question": str, "pending_command": str|None,
                                     "resume_messages": [...], "elapsed_seconds": float,
                                     "steps_used": int}
                     Every outcome also carries "elapsed_seconds"/"steps_used" so the caller
                     can pass them back in as elapsed_so_far/steps_so_far on the next call.
      transcript -- list of message/event dicts, JSON-serializable, suitable
                     for storage and for a capped excerpt in the task note. Unlike
                     "resume_messages" (the live conversation), this may include
                     non-message annotation entries (role "system", key "note").

    `resume_messages`, when given, continues an existing conversation (as
    returned in a prior "parked" outcome, with the human's reply already
    appended by the caller) instead of starting fresh from system_text/
    task_prompt -- `system_text`/`task_prompt` are then ignored.

    `ntfy_publish(message, attach_path)` backs the send_update tool; any
    exception it raises is caught and reported back to the model as a tool
    result, never propagated.

    `backend`, when given, overrides cfg.exec_backend for every `run` tool
    call this loop makes -- executor.py resolves it once per run (e.g.
    "mac" for a reachable Mac-routed project) and passes the same value on
    every resume, so it can't drift mid-run. See sandbox.run()'s docstring.

    `stop_check`, when given, is a zero-arg callable polled once at the top
    of every loop iteration (same turn-boundary as the total_timeout check
    below) -- if it returns true, the loop stops before starting the next
    turn and returns a "stopped" outcome. executor.py backs this with a
    background ntfy listener on the run's own topic, so a human can halt a
    run mid-chunk, not just between chunks. Never interrupts a call already
    in flight -- an in-progress `run` shell command still finishes.

    Parking (ask_question / an approval-needing `run` / a repeated-failure
    `run`) is only detected in the native tool-calling path, not the JSON
    fallback below -- a model that doesn't support tool calling at all has no
    way to distinguish a genuine pause from an ordinary action anyway, so the
    fallback just runs commands needing approval as deny (see sandbox.py),
    never dedupes repeated failures, and has no ask_question equivalent.

    `llm` is expected to already be constructed with the effective exec
    model/num_ctx (executor.py accounts for the prompt file's optional
    overrides there) -- this loop doesn't re-specify model/num_ctx per call,
    it just uses whatever the client was built with.
    """
    if resume_messages is not None:
        messages = list(resume_messages)
    else:
        messages = [
            {"role": "system", "content": f"{system_text}\n\nWORKSPACE_ROOT: {workspace_root}"},
            {"role": "user", "content": task_prompt},
        ]
    transcript = [dict(m) for m in messages]

    started = time.monotonic()

    def _elapsed():
        return elapsed_so_far + (time.monotonic() - started)

    no_tool_call_streak = 0
    json_fallback = False
    step = 0
    run_state = {"command": None, "streak": 0}

    remaining_steps = (max_steps or cfg.executor_max_steps) - steps_so_far
    for step in range(1, max(remaining_steps, 0) + 1):
        if _elapsed() > cfg.executor_total_timeout:
            log.warning("agent loop hit total_timeout after %s step(s) this call", step - 1)
            break

        if stop_check is not None and stop_check():
            log.info("agent loop stopped by user request after %s step(s) this call", step - 1)
            transcript.append({"role": "system", "note": "stopped by user request"})
            return {"status": "stopped", "report": dict(STOPPED_REPORT), "elapsed_seconds": _elapsed(),
                   "steps_used": steps_so_far + step}, transcript

        if not json_fallback:
            try:
                message = llm.chat_tools(messages, TOOLS, temperature=cfg.exec_temperature)
            except OllamaError as e:
                transcript.append({"role": "system", "note": f"chat_tools error: {e}"})
                break

            tool_calls = message.get("tool_calls") or []
            messages.append(message)
            transcript.append(dict(message))

            if not tool_calls:
                fallback_call = _parse_content_tool_call(message.get("content"))
                if fallback_call is not None:
                    name, args = fallback_call
                    no_tool_call_streak = 0
                    log.info("model wrote a well-formed %r call as content instead of "
                            "tool_calls -- dispatching it instead of discarding it", name)
                    transcript.append({"role": "system",
                                       "note": f"recovered {name!r} call from message content "
                                              "(tool_calls was empty)"})
                    outcome = _handle_tool_call(cfg, workspace_root, backend, ntfy_publish,
                                                messages, transcript, name, args, steps_so_far,
                                                step, _elapsed, run_state)
                    if outcome is not None:
                        return outcome, transcript
                    continue

                no_tool_call_streak += 1
                if no_tool_call_streak >= 2:
                    log.warning("model returned no tool_calls twice in a row, "
                               "switching to JSON action fallback")
                    json_fallback = True
                    continue
                messages.append({"role": "user", "content": NO_TOOL_CALL_NUDGE})
                continue
            no_tool_call_streak = 0

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name")
                args = _parse_args(fn.get("arguments"))
                outcome = _handle_tool_call(cfg, workspace_root, backend, ntfy_publish, messages,
                                            transcript, name, args, steps_so_far, step, _elapsed,
                                            run_state)
                if outcome is not None:
                    return outcome, transcript

        else:
            history = "\n".join(f"[{m.get('role')}] {json.dumps(m)[:500]}"
                                for m in messages[-8:])
            user = (f"Conversation so far:\n{history}\n\n"
                   "Respond with exactly one action as JSON: "
                   '{"tool": <name>, "args": {...}}.')
            try:
                action = llm.chat_json(messages[0]["content"], user, FALLBACK_SCHEMA,
                                       temperature=cfg.exec_temperature, num_predict=1200)
            except OllamaError as e:
                transcript.append({"role": "system", "note": f"fallback chat_json error: {e}"})
                break

            name, args = action.get("tool"), action.get("args") or {}
            transcript.append({"role": "assistant", "action": action})

            if name == "finish":
                return {"status": "done", "report": args, "elapsed_seconds": _elapsed(),
                       "steps_used": steps_so_far + step}, transcript

            result = _dispatch(cfg, workspace_root, backend, ntfy_publish, name, args)
            messages.append({"role": "user", "content": f"{name} result: {result}"})
            transcript.append({"role": "tool", "name": name, "content": result})

    return ({"status": "timed_out", "report": dict(TIMED_OUT_REPORT),
            "elapsed_seconds": _elapsed(), "steps_used": steps_so_far + step},
           transcript)

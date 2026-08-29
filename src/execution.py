"""Execution-pipeline logic: prompt loading, prompt assembly, report rendering.

planning.py's equivalent for executor.py, kept separate for the same reason
planning.py is kept separate from pipeline.py -- different frontmatter shape
(model/num_ctx/max_steps overrides and a finish()-report schema, not
effort), and a different rendered deliverable (an '## Execution' section
appended to an existing plan note, not the whole note).
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

import planning

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_OUT_OF_SCOPE_RE = re.compile(r"^\*\*Out of scope:\*\*\s*(.+)$", re.M)
_STEP_RE = re.compile(r"^\d+\.\s*(.+)$")
_CHECKBOX_RE = re.compile(r"^-\s*\[.\]\s*(.+)$")
_BULLET_FILE_RE = re.compile(r"^-\s*`(.+)`$")


@dataclass
class ExecPrompt:
    name: str
    system: str
    schema: dict
    model: str | None
    num_ctx: int | None
    max_steps: int | None


def load_prompt(path: Path) -> ExecPrompt:
    text = path.read_text()
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path} is missing a YAML frontmatter block")
    meta = yaml.safe_load(m.group(1)) or {}
    return ExecPrompt(
        name=meta.get("name", path.stem),
        system=m.group(2).strip(),
        schema=meta["schema"],
        model=meta.get("model"),
        num_ctx=meta.get("num_ctx"),
        max_steps=meta.get("max_steps"),
    )


def _block(**kv):
    out = []
    for k, v in kv.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, indent=2)
        out.append(f"{k}:\n{v}\n")
    return "\n".join(out)


def build_prompt(prompt: ExecPrompt, *, task_title, plan: dict, workspace_dir) -> str:
    plan = plan or {}
    steps = plan.get("steps") or []
    ac = plan.get("acceptance_criteria") or []
    files = plan.get("files_likely_touched") or []
    body = _block(
        TASK_TITLE=task_title,
        PLAN_APPROACH=plan.get("approach"),
        PLAN_STEPS="\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) if steps else None,
        ACCEPTANCE_CRITERIA="\n".join(f"- {x}" for x in ac) if ac else None,
        FILES_LIKELY_TOUCHED="\n".join(f"- {x}" for x in files) if files else None,
        OUT_OF_SCOPE=plan.get("out_of_scope"),
        WORKSPACE_DIR=str(workspace_dir),
    )
    return f"{prompt.system}\n\n{body}"


def plan_from_note(note: str) -> dict:
    """Fallback for when plan_queue.result_json isn't available for the task
    (e.g. the plan predates this feature, or the row was pruned) -- re-parses
    the '## Implementation steps' / '## Acceptance criteria' / etc. sections
    that planning.render_plan() itself writes. Good enough for build_prompt(),
    not a general Markdown plan parser: list-item numbering/checkbox markup
    is stripped, and anything not in one of these sections is simply absent,
    which build_prompt() already treats as "nothing to show" for that block.
    """
    if not note:
        return {}
    sections = {}
    matches = list(_SECTION_RE.finditer(note))
    for i, m in enumerate(matches):
        heading = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(note)
        sections[heading] = note[start:end].strip()

    plan = {}
    if "approach" in sections:
        plan["approach"] = sections["approach"]
    if "implementation steps" in sections:
        plan["steps"] = [mm.group(1) for line in sections["implementation steps"].splitlines()
                         if (mm := _STEP_RE.match(line.strip()))]
    if "acceptance criteria" in sections:
        plan["acceptance_criteria"] = [
            mm.group(1) for line in sections["acceptance criteria"].splitlines()
            if (mm := _CHECKBOX_RE.match(line.strip()))
        ]
    if "files likely touched" in sections:
        plan["files_likely_touched"] = [
            mm.group(1) for line in sections["files likely touched"].splitlines()
            if (mm := _BULLET_FILE_RE.match(line.strip()))
        ]
    m = _OUT_OF_SCOPE_RE.search(note)
    if m:
        plan["out_of_scope"] = m.group(1).strip()
    return plan


def render_report(report: dict, *, branch: str, diffstat: str, model: str, steps_used: int,
                  seconds: float, transcript_excerpt: str, executed_at: str,
                  backend_note: str = None) -> str:
    """Compose the '## Execution' section appended to the task's existing note.
    `backend_note`, when given (e.g. "Mac backend unreachable, fell back to
    docker -- iOS-specific work could not be verified"), is called out first
    so it isn't buried under the rest of the report."""
    report = report or {}
    parts = ["## Execution", f"**Branch:** `{branch}`"]

    if backend_note:
        parts.append(f"**Note:** {backend_note}")

    if diffstat:
        parts.append("**Diff:**\n```\n" + diffstat.strip() + "\n```")

    files = report.get("files_changed") or []
    if files:
        parts.append("**Files changed:**\n" + "\n".join(f"- `{x}`" for x in files))

    commands = report.get("commands_run") or []
    if commands:
        parts.append("**Commands run:**\n" + "\n".join(f"- `{x}`" for x in commands))

    checks = report.get("acceptance_check") or []
    if checks:
        marks = {"met": "x", "not_met": " ", "unverified": "?"}
        lines = []
        for c in checks:
            if isinstance(c, dict):
                note = f" -- {c['note']}" if c.get("note") else ""
                lines.append(f"- [{marks.get(c.get('status'), ' ')}] {c.get('criterion', '')}{note}")
            else:
                lines.append(f"- {c}")
        parts.append("**Acceptance self-check:**\n" + "\n".join(lines))

    if report.get("summary"):
        parts.append("**Agent summary:**\n" + report["summary"].strip())

    meta = [f"model {model}", f"{steps_used} step(s)", f"{seconds:.0f}s",
           f"confidence {report.get('confidence', '?')}"]
    parts.append(f"_executed {executed_at} · " + " · ".join(meta) + "_")

    if transcript_excerpt:
        parts.append("<details><summary>Transcript excerpt</summary>\n\n```\n" +
                     transcript_excerpt.strip() + "\n```\n</details>")

    return "\n\n".join(parts)


def derive_exec_tags(current_tags: list, owned: set, new_status: str) -> list:
    """Same owned-tag-subtraction rule as planning.derive_plan_tags -- generic
    enough (strip tags this daemon owns, append new_status, leave everything
    else) to reuse directly rather than duplicate."""
    return planning.derive_plan_tags(current_tags, owned, new_status)

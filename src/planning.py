"""Plan-pipeline logic: prompt loading, prompt assembly, plan rendering.

This feature's pipeline.py equivalent, kept separate rather than extending
that file -- the frontmatter fields differ (effort, not temperature/
num_predict; the CLI manages its own generation length, so --max-budget-usd
is the cost/length safety net instead), and the rendered document is a
different, richer deliverable (a scoped plan, not a triage summary).
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class ClaudePrompt:
    name: str
    system: str
    schema: dict
    effort: str


def load_prompt(path: Path) -> ClaudePrompt:
    text = path.read_text()
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path} is missing a YAML frontmatter block")
    meta = yaml.safe_load(m.group(1)) or {}
    return ClaudePrompt(
        name=meta.get("name", path.stem),
        system=m.group(2).strip(),
        schema=meta["schema"],
        effort=meta.get("effort", "high"),
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


def build_prompt(prompt: ClaudePrompt, *, task_title, task_note, project_notes,
                  repo_dir, clarification_history, final_round) -> str:
    """Composes the full text passed to `claude -p`. The CLI takes a single
    prompt string (no separate system/user split like the Messages API), so
    the prompt file's system text is prepended to the context blocks."""
    body = _block(
        TASK_TITLE=task_title,
        TASK_NOTE=task_note,
        PROJECT_NOTES=project_notes,
        REPO_DIR=(f"{repo_dir} -- explore it with Read/Grep/Glob before answering"
                  if repo_dir else "(no repo configured for this project)"),
        CLARIFICATION_HISTORY=clarification_history,
        FINAL_ROUND=final_round,
    )
    return f"{prompt.system}\n\n{body}"


def render_plan(plan: dict, task_note: str, repo_label, conversation: dict,
                model: str, effort: str, cost_usd, planned_at: str) -> str:
    """Compose the task note. The verbatim original request is ALWAYS last."""
    parts = []

    if plan.get("approach"):
        parts.append("## Approach\n" + plan["approach"].strip())

    chunks = plan.get("chunks") or []
    if len(chunks) > 1:
        parts.append("## Execution chunks\n" + "\n".join(
            f"{i}. **{c.get('title') or f'Chunk {i}'}** "
            f"({len(c.get('steps') or [])} step(s))"
            for i, c in enumerate(chunks, 1)
        ))

    steps = plan.get("steps") or []
    if steps:
        parts.append("## Implementation steps\n" +
                     "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))

    ac = plan.get("acceptance_criteria") or []
    if ac:
        parts.append("## Acceptance criteria\n" +
                     "\n".join(f"- [ ] {x}" for x in ac))

    files = plan.get("files_likely_touched") or []
    if files:
        parts.append("## Files likely touched\n" +
                     "\n".join(f"- `{x}`" for x in files))

    risks = list(plan.get("risks") or [])
    unresolved = conversation.get("unresolved_questions") or []
    risks += [f"Unresolved: {q}" for q in unresolved]
    if risks:
        parts.append("## Risks / open questions\n" +
                     "\n".join(f"- {x}" for x in risks))

    if plan.get("out_of_scope"):
        parts.append(f"**Out of scope:** {plan['out_of_scope']}")

    parts.append("## Repo grounding\n" +
                 (f"`{repo_label}` — local clone was available for Claude to explore."
                  if repo_label else "None configured for this project."))

    rounds = conversation.get("rounds") or []
    if rounds:
        history = "\n\n".join(
            f"**Round {i} question:** {r['question']}\n**Answer:** {r['answer']}"
            for i, r in enumerate(rounds, 1)
        )
        parts.append("## Clarification history\n" + history)

    cost = f" · ${cost_usd:.4f}" if cost_usd else ""
    parts.append(
        "---\n## Original request\n> " + task_note.replace("\n", "\n> ") +
        f"\n\n_planned {planned_at} · model {model} · effort {effort}{cost} · "
        f"{len(rounds)} clarification round(s)_"
    )

    return "\n\n".join(parts)


def derive_plan_tags(current_tags: list, owned: set, new_status: str) -> list:
    """Removes only tags the planner owns (the trigger tag + every plan:*
    status tag, passed in as `owned`) from current_tags, then appends
    new_status. Everything else the user or the triage pipeline set is
    preserved -- unlike worker.py's tag handling, which always sends a full
    replacement list because a capture stub only ever has one throwaway tag;
    this touches real, user-managed tasks, so clobbering isn't safe here."""
    kept = [t for t in (current_tags or []) if t not in owned]
    kept.append(new_status)
    return kept

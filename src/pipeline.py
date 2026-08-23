"""Three-pass enrichment pipeline.

    classify -> draft -> critique

Each pass lives in a Markdown file with a YAML frontmatter block holding its
schema and sampling settings, so prompts can be iterated without touching code.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class Prompt:
    name: str
    system: str
    schema: dict
    temperature: float
    num_predict: int


def load_prompt(path: Path) -> Prompt:
    text = path.read_text()
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path} is missing a YAML frontmatter block")
    meta = yaml.safe_load(m.group(1)) or {}
    return Prompt(
        name=meta.get("name", path.stem),
        system=m.group(2).strip(),
        schema=meta["schema"],
        temperature=float(meta.get("temperature", 0.1)),
        num_predict=int(meta.get("num_predict", 800)),
    )


def load_all(prompt_dir: Path):
    return {
        "classify": load_prompt(prompt_dir / "01_classify.md"),
        "draft": load_prompt(prompt_dir / "02_draft.md"),
        "critique": load_prompt(prompt_dir / "03_critique.md"),
    }


def _block(**kv):
    out = []
    for k, v in kv.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, indent=2)
        out.append(f"{k}:\n{v}\n")
    return "\n".join(out)


def run(llm, prompts, *, raw_text, project_name, project_notes,
        hint_tags, hint_priority, log=print):
    """Returns (final_dict, telemetry_dict). Raises on unrecoverable failure."""
    telemetry = {}

    # ---- pass 1: classify ----
    p = prompts["classify"]
    user = _block(PROJECT=project_name, RAW=raw_text,
                  HINT_TAGS=hint_tags, HINT_PRIORITY=hint_priority)
    classification = llm.chat_json(p.system, user, p.schema,
                                   p.temperature, p.num_predict)
    telemetry["classification"] = classification
    log(f"  classify: type={classification.get('type')} "
        f"size={classification.get('size')} "
        f"confidence={classification.get('confidence')}")

    # ---- pass 2: draft ----
    p = prompts["draft"]
    user = _block(PROJECT=project_name, PROJECT_NOTES=project_notes,
                  RAW=raw_text, CLASSIFICATION=classification)
    draft = llm.chat_json(p.system, user, p.schema, p.temperature, p.num_predict)
    telemetry["draft"] = draft
    log(f"  draft: {draft.get('title','')[:70]!r}")

    # ---- pass 3: critique ----
    p = prompts["critique"]
    user = _block(RAW=raw_text, DRAFT=draft)
    try:
        review = llm.chat_json(p.system, user, p.schema, p.temperature, p.num_predict)
        final = review.get("corrected") or draft
        telemetry["issues"] = review.get("issues", [])
        telemetry["hallucinations"] = review.get("hallucinations", [])
        log(f"  critique: {len(telemetry['issues'])} issue(s), "
            f"{len(telemetry['hallucinations'])} hallucination(s)")
    except Exception as e:
        # The critique pass is a quality gate, not a hard dependency.
        log(f"  critique failed, keeping draft: {e}")
        final = draft
        telemetry["issues"] = [f"critique pass failed: {e}"]

    final["_classification"] = classification
    return final, telemetry


def render_description(final: dict, raw_text: str, topic: str, captured_at: str) -> str:
    """Compose the task body. The verbatim capture is ALWAYS preserved last."""
    c = final.get("_classification", {})
    parts = []

    if final.get("context"):
        parts.append(final["context"].strip())

    ac = final.get("acceptance_criteria") or []
    if ac:
        parts.append("## Acceptance criteria\n" +
                     "\n".join(f"- [ ] {x}" for x in ac))

    oq = final.get("open_questions") or []
    if oq:
        parts.append("## Open questions\n" + "\n".join(f"- {x}" for x in oq))

    sf = final.get("suggested_files") or []
    if sf:
        parts.append("## Likely files\n" + "\n".join(f"- `{x}`" for x in sf))

    if final.get("out_of_scope"):
        parts.append(f"**Out of scope:** {final['out_of_scope']}")

    meta = [f"type: {c.get('type','?')}",
            f"size: {c.get('size','?')}",
            f"confidence: {c.get('confidence','?')}"]
    parts.append("---\n## Original capture\n> " +
                 raw_text.replace("\n", "\n> ") +
                 f"\n\n_via ntfy `{topic}` at {captured_at} · " +
                 " · ".join(meta) + "_")

    return "\n\n".join(parts)


def derive_tags(final: dict) -> list:
    c = final.get("_classification", {})
    tags = ["auto-triaged"]
    if c.get("type"):
        tags.append(f"type:{c['type']}")
    if c.get("size") and c["size"] != "unknown":
        tags.append(f"size:{c['size']}")
    if c.get("confidence") == "low" or not c.get("actionable", True):
        tags.append("needs-refinement")
    else:
        tags.append("ready")
    return tags

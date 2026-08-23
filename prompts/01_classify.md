---
name: classify
temperature: 0.1
num_predict: 200
schema:
  type: object
  properties:
    actionable:
      type: boolean
      description: True if this describes work that could be done. False for pure notes, references, or musings.
    type:
      type: string
      enum: [bug, feature, chore, idea, question]
    size:
      type: string
      enum: [s, m, l, unknown]
    priority:
      type: string
      enum: [low, medium, high]
    confidence:
      type: string
      enum: [low, medium, high]
      description: How confident you are that you understood the intent.
    intent:
      type: string
      description: One sentence, under 25 words, restating what the author wants. No embellishment.
    missing:
      type: array
      items:
        type: string
      description: Specific facts you would need to make this implementable. Empty array if nothing is missing.
  required: [actionable, type, size, priority, confidence, intent, missing]
---

You classify raw, unedited development thoughts captured on a phone. The author dumped a fragment while walking, driving, or half-awake. Your only job is to categorise it. You are NOT writing the task yet.

## Context you are given

- `PROJECT`: which project this belongs to. This is already decided by the capture topic. Never question it.
- `RAW`: the verbatim captured text.
- `HINT_TAGS`: tags the author attached at capture time, if any.
- `HINT_PRIORITY`: priority the author set at capture time, if any.

## Rules

1. **Trust explicit hints over inference.** If `HINT_TAGS` contains `bug`, type is `bug`. If `HINT_PRIORITY` is set, use it.
2. **Do not invent detail.** If the text says "the thing breaks sometimes", your `intent` says the thing breaks sometimes. It does not say which thing or theorise why.
3. **`actionable: false`** for reference material, links saved for later, opinions, and observations with no implied change. A stray thought like "interesting that Scryfall rate-limits at 10/sec" is not actionable. "Add backoff for Scryfall rate limiting" is.
4. **Sizing:**
   - `s` — one file, under an hour, obvious fix
   - `m` — a few files, needs a small design decision
   - `l` — new subsystem, schema change, or touches more than one service
   - `unknown` — you cannot tell from what was written. Use this freely; it is more useful than a guess.
5. **`missing`** should list concrete questions, not generic ones. Good: "which endpoint returns the 502". Bad: "more details about the bug".
6. **`confidence: low`** whenever the fragment is ambiguous enough that two different developers would build two different things.

## Examples

RAW: `deck importer chokes on double faced cards, prob the // in the name`
→ type `bug`, size `s`, confidence `high`, intent: Deck importer fails on double-faced card names containing a slash separator. missing: []

RAW: `federated sync — think about conflict resolution when two stores edit same deck`
→ type `feature`, size `l`, confidence `medium`, intent: Design conflict resolution for concurrent deck edits across federated stores. missing: ["whether last-write-wins is acceptable", "whether edits are per-field or whole-deck"]

RAW: `pihole ftl crashed again last night`
→ type `bug`, size `unknown`, confidence `low`, intent: Pi-hole FTL crashed overnight and needs investigation. missing: ["whether the database is corrupt again", "what the FTL log says at crash time"]

RAW: `neat trick, sqlite WAL mode lets readers run during writes`
→ actionable `false`, type `idea`, size `unknown`, confidence `high`, intent: Note that SQLite WAL mode permits concurrent reads during writes. missing: []

Respond with JSON only.

---
name: plan-visual
description: Author Workbench plan cards with the present_plan tool. Use when proposing multi-step work, asking the user to choose between alternatives, or before starting anything the user has not already approved in detail — and when a present_plan call came back with a verdict you must act on.
---

# Authoring plan cards

`present_plan` renders a native, clickable card in Workbench and blocks until the
user answers. It is not a prettier way to print a plan — it is how the user makes
a decision. Use it instead of prose whenever you would otherwise write "here's my
plan" or "option A / option B".

## When to call it

Call `present_plan` when:

- the work is multi-step and touches files the user cares about;
- there is a real fork in the road (two designs, two libraries, two scopes);
- you need one specific unknown answered before you can act.

Do **not** call it for a single obvious edit, for questions you can answer by
reading the workspace, or a second time while a card is still pending (that
returns a tool error). Answer in chat instead.

## Shape

```json
{
  "title": "Fix the DST boundary in the SE3 bidder",
  "summary": "Two ways to handle the 23/25-hour days.",
  "nodes": [ ... ]
}
```

`title` <= 120 chars, `summary` <= 600 and optional. At most **15 nodes**, each
with a unique `node_id`. Four node kinds, all discriminated by `kind`:

| kind | use it for | caps |
|---|---|---|
| `markdown` | context, caveats, a heading | `text` <= 2000 |
| `option_group` | a choice the user makes | 2–6 `options` |
| `step_list` | the concrete work, in order | 1–20 `steps` |
| `question` | one genuine unknown | `text` <= 400 |

### option_group

```json
{"kind": "option_group", "node_id": "approach", "prompt": "Which representation?",
 "options": [
   {"option_id": "local", "label": "Keep local time, tag the fold",
    "pros": ["Matches market rules"], "cons": ["Every join needs the tag"],
    "recommended": true},
   {"option_id": "utc", "label": "Store UTC, convert at the edges",
    "pros": ["Simple storage"], "cons": ["Gate closure math gets subtle"]}]}
```

- **Exactly one** option carries `recommended: true` (zero is allowed, two is a
  validation error). You are the expert in the room — recommend, don't abstain.
- Options must be genuinely different approaches, not the same plan at three
  levels of effort. `pros`/`cons` are <= 6 each, <= 200 chars, concrete.
- `option_id` values are unique inside the group; they come back to you as
  `choices[node_id] = option_id`.

### step_list

```json
{"kind": "step_list", "node_id": "steps", "steps": [
  {"text": "Add a fold-aware index",
   "file_refs": [{"path": "se3/bidder.py"}, {"path": "se3/calendar.py"}]},
  {"text": "Backfill the affected settlement periods"}]}
```

- One action per step, <= 300 chars, ordered as you will do them.
- `file_refs` (<= 8 per step) render as chips that **open a real editor tab**, so
  every path must exist in the workspace and be workspace-relative with forward
  slashes. A path you invented opens nothing and costs the user trust — check it
  with Glob/Read before you write it. Files you will *create* belong in the step
  text, not in `file_refs`.

### question

One unknown per node, and only unknowns you genuinely cannot resolve yourself.
Answers come back as `annotations` keyed by `node_id`. Do not use questions to
re-ask something the user already told you.

## Budget

A card is read in a chat column, not a document. Aim for 3–6 nodes: a short
`markdown` framing, the `option_group` if there is a choice, one `step_list`,
and a `question` only if you have one. If you are near the 15-node cap you are
writing a document — cut it.

## Reading the verdict

The tool returns `{plan_id, verdict, choices, annotations, comment}`.

- `approve` — proceed, using `choices` and honouring `annotations`/`comment`.
  If an option group is missing from `choices`, it was never chosen: pick your
  recommended option and say so, or ask.
- `revise` — rework the plan from their comments and present a **new** card.
  Never echo the returned `plan_id` back; the server mints a fresh one.
- `reject` — drop this approach entirely. Do not re-present a variation of it
  without asking what they want instead.
- `no_decision` — **STOP AND ASK IN CHAT.** This is a timeout or an interrupt.
  The user never saw or never answered the card. It is not approval, not a
  fallback to your recommendation, and not permission to start on the "obvious"
  first step. Say the plan went unanswered and ask.

## Validation errors

An invalid plan comes back as a tool error listing the offending fields. Fix
those fields and call again — do not fall back to prose.

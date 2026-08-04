---
name: remember
description: Persist durable workspace knowledge to .workbench/memory.md, and read it back. Use when the user says remember this, note this for next time, or states a decision, convention, gotcha or preference that should outlive the session — and at the start of work in an unfamiliar workspace, to read what is already known.
---

# Workspace memory

Durable facts about *this workspace* live in `.workbench/memory.md`, relative to
the workspace root. It is a plain markdown file on disk: the user can read it,
edit it, diff it, and delete it. Nothing is hidden in a database.

## Read before you write

Always `Read` `.workbench/memory.md` first (it may not exist yet — that is fine).
Two reasons: you avoid writing a fact that is already there, and you find the
entry that is now *wrong* and should be corrected instead of duplicated.

At the start of substantial work in a workspace you have not seen this session,
read it unprompted. That is the whole point of writing it down.

## What belongs

Facts that will still be true next week and that a fresh session would otherwise
have to rediscover:

- **Decisions** — "prices are stored in EUR/MWh, never EUR/kWh"; "we settled on
  OnlyOffice over Univer because xlsx export is Pro-only there".
- **Conventions** — naming, layout, which script is the entrypoint, how to run
  the tests in *this* repo.
- **Gotchas** — "the vendor CSV has a 25-hour day on the DST fold and repeats the
  hour label"; "`build.ps1` must run from the repo root or it silently no-ops".
- **Stated preferences** — "the user wants diffs, not summaries".

## What does not

- Session chatter, task status, TODOs, "I am now editing X".
- Anything you inferred but did not verify. Uncertain notes rot into wrong ones.
- Anything already obvious from the code or from `CLAUDE.md`/`README.md`. Memory
  duplicating a tracked file is a second source of truth that will drift.
- **Never secrets.** No API keys, tokens, passwords, connection strings, or
  personal paths. If the user tells you one, do not write it anywhere. Refer to
  it by name ("the DB password is in the `.env`, key `PGPASSWORD`") instead.

## Format

One fact per bullet, dated, newest at the bottom of its section. Create the file
with this skeleton if it is missing (`mkdir` the `.workbench` directory first):

```markdown
# Workspace memory

Durable facts about this workspace, written by Claude via the `remember` skill.
Edit or delete freely — this file is yours.

## Decisions

- 2026-08-04 — Prices are stored in EUR/MWh everywhere; the vendor feed's
  EUR/kWh values are converted at ingest, not at read time.

## Conventions

- 2026-08-04 — Tests run with `uv run pytest` from the repo root.

## Gotchas

- 2026-08-04 — `data/spot.csv` has a 25-hour day on the autumn DST fold; the
  duplicate hour label is not a bug in the file.
```

Rules for an entry:

- Start with the ISO date, then an em dash, then the fact.
- State the fact, and *why* if the why is the useful part. One or two sentences.
- Write it so it reads correctly with no conversation around it. "The user said
  yes" is useless; "Backfills are approved for 2025 only" is a fact.
- Correct a stale entry by editing it in place and re-dating it. Delete entries
  that are no longer true — an outdated memory is worse than none.
- Use the four headings above; add a new heading only when a fact fits none.

## Writing it

Use `Edit` to append under the right heading (or `Write` for a new file). Keep
the diff to the entry you are adding. Disk is the source of truth in Workbench:
the watcher will show the change in the file tree immediately, and the user may
have edited the file since you last read it — so re-read before a second write
in the same session.

Tell the user in one line what you recorded and where. Do not paste the whole
file back at them.

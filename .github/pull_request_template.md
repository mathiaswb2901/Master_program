## What changed

<!-- User-visible behavior, one or two sentences. Link the ROADMAP item or issue. -->

## How it was verified

<!--
Test names for logic; for UI, the manual steps plus a screenshot. Say what you
actually observed, including anything that did not work — "CI is green" alone is
not verification, and an honest gap costs far less than a discovered one.
-->

## Always

- [ ] New behavior has tests (cross-module behavior → full-pipeline test)
- [ ] Gates run locally: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`, and in `ui/`: `npm run lint && npm run test && npm run build`
- [ ] Branch is up to date with `master` (the quality gate requires it)
- [ ] No secrets, personal paths, or machine-specific config
- [ ] No new runtime dependency — or the description above justifies it
- [ ] Nothing added that collects usage data, reports crashes, or calls home

## If it applies

- [ ] Every new/changed wire payload is a Pydantic model, mirrored in `ui/src/types.ts`
- [ ] UI changes use only `tokens.css` variables and follow `DESIGN.md`
- [ ] A new capability registers itself (its own module + one line in `ui/src/tools.ts`) — no edits to `App.tsx`, `commands.ts` or `StatusBar.tsx`
- [ ] A plural tool ships the two-instances test: independent through a save/restore round trip
- [ ] Nothing needed only on demand became a static import from `main.tsx` (`ui/e2e/perf/bundle.spec.ts`)
- [ ] A new agent-facing tool has a short description and its own test asserting the description and result-size ceilings
- [ ] A bug fix started from an end-to-end reproduction, and that repro is the regression test
- [ ] Office-file handling preserves round-trip fidelity (`.bak` + reopen check)

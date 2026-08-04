## What changed

<!-- User-visible behavior, one or two sentences. Link the ROADMAP item or issue. -->

## How it was verified

<!-- Test names for logic; for UI: manual steps + screenshot. "CI is green" alone is not verification. -->

## Checklist

- [ ] New behavior has tests (cross-module behavior → full-pipeline test)
- [ ] Every new/changed wire payload is a Pydantic model, mirrored in `ui/src/types.ts`
- [ ] UI changes use only `tokens.css` variables and follow `DESIGN.md`
- [ ] No new runtime dependency — or the PR description justifies it
- [ ] No secrets, personal paths, or machine-specific config
- [ ] Office-file handling preserves round-trip fidelity (`.bak` + reopen check) — or N/A

---
name: Bug report
about: Something behaves differently than it should
title: ""
labels: bug
assignees: ""
---

<!--
Before posting: please redact. Logs and paths from this app can contain your
folder names and, if you run OnlyOffice, its JWT secret. Nothing here needs a
secret, and an issue is public forever.
-->

## What happened

<!-- One or two sentences. What did you see? -->

## What you expected

## How to reproduce it

<!--
End to end, the way you hit it — the click path, not a guess at the cause.
1.
2.
3.
-->

## Where

- **Host:** desktop shell (`npm run tauri dev` / installed build) · browser tab (`npm run dev`) · not sure
- **OS and version:**
- **Surface:** files/editor · terminal · agent or chat · Office host (real Word/Excel docked) · OnlyOffice preview · QuickBar · Mission Control · search · other:
- **Version or commit:**
- **Python / Node / uv versions** (if you are running from source):

## Does it still happen with the fakes?

<!--
This one line saves a lot of round trips, because it separates "the app is
wrong" from "the environment is".

  WORKBENCH_FAKE_AGENT=1   scripted agent — no Claude login involved
  WORKBENCH_OFFICE_FAKE=1  the whole Office-host lifecycle with no Office installed

Answer "yes", "no", or "not applicable".
-->

## Logs

<!--
Backend: the output of `uv run workbench-server`.
Desktop shell: `shell.log` in the app's own log directory under %LOCALAPPDATA%.
Paste the relevant lines only, and redact folder names you would rather not publish.
-->

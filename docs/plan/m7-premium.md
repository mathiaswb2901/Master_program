# M7 — Premium & Public: the visual identity, first-run, voice, and the road to a public release

Status: **plan** (this document is the milestone's Plan PR; no feature code lands with
it). It turns the ROADMAP's M7 "Premium & Public" bullet into a sequence of disjoint,
fake-first, independently shippable PRs with explicit file ownership, so parallel lanes
never collide — the same shape as the M6 plan (`docs/plan/m6-proof.md`, #79). It is
written against the repo as it stands after M4–M6: the tool registry, panes, provenance,
Mission Control, the worktree pool, the usage service, the Office host, the validation
pipeline, and — the piece this milestone builds *on top of* — **ANVIL's colour half,
already landed** (`DESIGN.md` §2, `ui/src/design/tokens.css`, `ui/e2e/palette.test.ts`).

This is the last milestone: it makes the app **distinctive** and **publishable**. The
north-star instrument is built; M7 is where it stops looking like every other editor and
becomes something a stranger can install on a fresh machine and trust in under ten minutes.

## The one governing fact: ANVIL is continued, never restarted

The "frontend is too plain" change request has already executed its hardest half. Six
directions were drafted against a measured crispness bar and the owner chose **ANVIL** —
true black, achromatic neutrals (Lab C\* = 0.00 everywhere), one hot amber spent only on
*where I am* / *what is changing now*, an inverted surface ramp (chrome lighter than the
wells it frames), and a document mat that makes a docked Word page read as paper. That is
`DESIGN.md` §2, and **`ui/e2e/palette.test.ts` re-derives every published figure from
`tokens.css` and fails the build when one drifts.**

So M7's visual work has a hard constraint the M6 plan did not: **it invents no colour and
changes no token value.** Everything below is composition, spacing, motion, hierarchy and
micro-interaction *within the ANVIL palette* — the surfaces the change request was actually
about (panes, tabs, the dock, empty states, the QuickBar, toasts, the status bar) dressed
to the identity the tokens already encode. The three tests that already exist are the
guardrail every visual PR is measured against, not a thing any of them may weaken:

- **`ui/e2e/palette.test.ts`** — every colour figure is derived from `tokens.css`. A raw
  hex in a component or a stylesheet is a review failure (house rule); a new colour is a
  token PR against `DESIGN.md` §2, which is *out of M7 scope by the owner's own choice*.
- **The motion conformance test** (perf lane, `DESIGN.md` §5): fails the build on an
  animated layout property, a `transition: all`, a static `will-change`, or a hover that
  eases in. Every micro-interaction M7 adds is spring-based (`ui/src/design/springs.ts`),
  two-channel (travel = transform, tint = opacity/colour), and honours
  `prefers-reduced-motion` by zeroing the travel — for free, if it uses the vocabulary.
- **`ui/e2e/captionContrast.test.ts`** + `DESIGN.md` §7 — the contrast floor. Nothing M7
  dresses may drop text below its measured ratio.

## Design constraints carried from the house rules

Every PR below inherits these without restating them:

- **Typed payloads only.** Every REST/WS body is a Pydantic model in
  `server/src/workbench_server/models/`, mirrored in `ui/src/types.ts`. `mypy --strict`,
  ruff, pytest green; new behaviour ships with tests.
- **Thin routers, logic in services.** `routers/*.py` validate and delegate;
  `services/*.py` hold the work. structlog only, never `print`. `pathlib` for paths.
- **Registered capabilities.** A UI capability is one module (`ui/src/panels/X.tsx` +
  `ui/src/x.ts`) plus one line in `ui/src/tools.ts` — never an edit to `App.tsx`,
  `commands.ts` or `StatusBar.tsx`. DESIGN.md tokens, zero raw hex.
- **Nothing not needed to paint is statically imported.** Every *feature* M7 adds (voice,
  content search, settings, first-run) loads behind a dynamic `import()` and is warmed on
  idle — `ui/e2e/perf/bundle.spec.ts` asserts what is inside the entry chunk. A visual PR
  that only restyles existing surfaces adds no entry weight; a PR that adds a capability
  proves it stayed out of the launch path. This is the single rule most likely to be
  violated by a "premium" milestone, so it is called out on every capability PR below.
- **Plural by default, no singleton assumption.** Nothing M7 adds stores itself as
  `store.activeX`. First-run state, a voice session, a search result set — each is keyed
  by its own id or lives in the module that owns it.
- **Agent-tool byte budgets + the AXI three shapes.** Any agent-facing tool M7 adds (only
  content search plausibly does) is an `AgentToolSpec` in `services/agent_tools.py` with a
  measured `max_result_bytes` / `max_schema_bytes`, a short description, and its own test
  asserting the ceilings; every result truncates with a stated size, says "none"
  explicitly, and ends with the obvious next step.

## Using the `ui-ux-pro-max` skill, without a dependency

The `ui-ux-pro-max` skill — available to the authoring agent, not part of the app's own
session-scoped skill bundle — is a **design-intelligence source**, not a component library
and not a runtime. It is consulted *during authorship* of the visual PRs — for layout systems, spacing rhythm,
component composition patterns, motion presets (GSAP-shaped ideas), accessibility
guidelines and empty-state patterns — and its output is then **translated into ANVIL's
existing primitives**:

- Its colour palettes and font pairings are **not** adopted — ANVIL's tokens and
  `DESIGN.md` §3 typography already decide those, and `palette.test.ts` forbids a second
  source of truth. The skill informs *hierarchy and proportion*, never hue.
- Its GSAP motion presets are read as *intent* (what should ease, how far, in what
  sequence) and re-expressed as `ui/src/design/springs.ts` `linear()` tokens — never by
  importing GSAP or any animation runtime. The motion conformance test is what proves the
  translation stayed inside the vocabulary.
- Nothing it suggests introduces an npm dependency (house rule). The deliverable of every
  visual PR is self-contained, tokenised CSS + component composition. If the skill's
  pattern needs a library, the pattern is re-implemented against tokens or dropped.

The skill is cited in each visual PR's body as the design source, the way the M6 plan
cited `kunchenguid/axi` — **principles applied, nothing installed.**

---

## 1. The visual identity overhaul — surface-scoped, disjoint CSS

The overhaul is **not one PR.** A single "make it premium" branch would touch every
stylesheet in `ui/src/styles/` and collide with everything. Instead it is a set of
**surface-scoped PRs, each owning a disjoint set of CSS files and the components that carry
their class names**, so several design lanes run at once. The per-capability stylesheets
already exist and are already disjoint — that split is the seam.

Each visual PR ships the same three-part proof: (a) the surface restyled to ANVIL within
the guardrail tests; (b) its micro-interactions as spring tokens; (c) an updated or new
Playwright screenshot/assertion in the surface's existing `ui/e2e/*.spec.ts` so the look is
regression-guarded, not just eyeballed. **None of them adds a colour or a dependency.**

### V1 — The dock, tab strips and panel chrome

- **Owns:** `ui/src/styles/dockview.css`, `ui/src/styles/panes.css`, and the class names in
  `ui/src/panels/EditorArea.tsx` / `Terminal.tsx` tab strips (the nested strips, `DESIGN.md`
  §2.1) — **not** the panel bodies (those are the individual capability PRs). Tests:
  `ui/e2e/layout.spec.ts`, `ui/e2e/panes.spec.ts`.
- **Builds:** the pane focus rule (§6.1's 2px full-bleed amber), tab fusion with the
  surface below, the sash hairline weight (`--border-strong` in the dock gap), the split
  affordance's hover feel (§6.11), and the pane-entering motion (opacity + 0.985→1 scale
  over `--duration-2`). This is the frame everything else sits in, so it is **V1** —
  later surface PRs inherit its chrome values rather than re-deciding them.
- **CI-verifiable:** fully. The look is asserted through the existing dock/pane E2E specs
  plus the motion conformance test; no owner, no real Office.

### V2 — Empty states and the first-paint / welcome surface

- **Owns:** `ui/src/styles/app.css`'s empty-state block, the empty-state anatomy shared by
  panels (§6.10), and the welcome-card styling *inside* `ui/src/panels/Keyboard.tsx` (§6.13
  — the card already exists and is already the top of that panel; V2 dresses it, it does not
  rebuild it). Tests: `ui/e2e/discover.spec.ts`.
- **Builds:** branded empty states across every panel (centred, 260px, 32px icon, the
  named shortcut with keycaps), the welcome card as the product's most important empty
  state, and the first-paint feel — the app's face before any content lands. This composes
  with, and does not duplicate, the discoverability work (M5 item 17): the welcome card and
  keyboard reference are generated from the registry and V2 restyles that output.
- **CI-verifiable:** fully — `discover.spec.ts` already drives the welcome/empty states.

### V3 — The QuickBar and command-palette feel

- **Owns:** `ui/src/styles/quickbar.css`, `ui/src/styles/overlays.css`, and class names in
  `ui/src/panels/QuickBar.tsx`. Tests: `ui/e2e/quickbar.spec.ts`, `ui/e2e/commands.spec.ts`.
- **Builds:** the one overlay language of the app (§6.5) at premium quality — the 640px
  surface, 40px rows, keycap rendering, fuzzy-match highlight, pick-mode sections (§6.11),
  and its enter/leave motion as tint + a short travel, reduced-motion-safe. Because it is
  the *only* overlay (the discovery section forbids a second), getting it right lifts every
  gesture that routes through it: the command palette, the pane picker, the workspace
  switcher, the layout switcher.
- **CI-verifiable:** fully.

### V4 — The status bar and toasts

- **Owns:** `ui/src/styles/statusbar.css`, the toast styling (in `ui/src/panels/Toasts.tsx`
  + its rules in `overlays.css` — **coordinate the `overlays.css` boundary with V3**: V3
  owns the QuickBar/menu rules, V4 owns the toast rules; if that split is too fine, V4 lands
  after V3 and rebases, stated so it is not rediscovered). Tests: `ui/e2e/status.spec.ts`.
- **Builds:** the quiet-bar doctrine (§6.7 — items hide at zero) at premium finish: the
  session chips, the layout/keys chips at the outer edge, the attention badge, and the
  toast layer for currently-silent failures. Toasts enter on tint + travel and honour
  reduced motion. The status bar is the one always-visible chrome strip, so its polish is
  load-bearing for the "does not look like every editor" test.
- **CI-verifiable:** fully.

### V5 — The agent + chat surface

- **Owns:** `ui/src/styles/agent.css`, `ui/src/styles/conversations.css`, `plan.css`, and
  the class names in `ui/src/panels/Chat.tsx` / `AgentPanel.tsx` / `Conversations.tsx` /
  `PlanCard.tsx`. Tests: `ui/e2e/chat.spec.ts`, `ui/e2e/plan.spec.ts`,
  `ui/e2e/conversations.spec.ts`.
- **Builds:** the chat message/tool-call/permission-prompt hierarchy (§6.3), the plan-card
  finish, the session picker's reserved box (§6.12, already shipped structurally in #61 —
  V5 dresses it), and the agent-status dot vocabulary (§2.6). This is the surface the user
  stares at most, and the one that most reads as "a fleet of agents" rather than "a text
  box", which is the north star made visible.
- **CI-verifiable:** fully, against `WORKBENCH_FAKE_AGENT=1`.

### V6 — The document mat and Office surfaces

- **Owns:** `ui/src/styles/office.css` and the class names in `ui/src/panels/OfficePanel.tsx`
  / `OfficeHostPanel.tsx` **that are pure CSS/markup** — it does **not** touch
  `desktop/**` or `services/office_host` (in-flight lane). Tests: `ui/e2e/office.spec.ts`,
  `ui/e2e/documents.spec.ts`.
- **Builds:** the paper mat (§2.1 `--surface-paper-surround`, §6.1) that makes a docked
  page read as paper, the paper-rim framing, the degraded-mode card, and the loading state
  on paper colours (§6.10). The moat's surface deserves the milestone's best finish.
- **CI-verifiable:** fully, via `WORKBENCH_OFFICE_FAKE=1`.

### V7 — Monaco enrichment and content search (Ctrl+Shift+F)

Two ROADMAP-named M7 features that are *capabilities*, not restyles, folded here because
they dress the editor surface:

- **Monaco enrichment** — theme the editor to ANVIL (a Monaco theme derived from the same
  tokens, `DESIGN.md` §2.8 already specifies the harmonisation), no raw hex, loaded on the
  existing Monaco dynamic-import path (`ui/src/monacoBundle.ts`) so it adds **zero entry
  weight**. Owns `ui/src/monaco.ts` theme wiring + `ui/src/styles/editor.css`.
- **Content search** — a registered capability (`ui/src/panels/Search.tsx` + `ui/src/search.ts`
  + one line in `tools.ts`; server `models/search.py`, `services/search.py`,
  `routers/search.py`) bound to `Ctrl+Shift+F`, searching within the jailed workspace. Its
  agent-facing tool (`workspace_search`, an `AgentToolSpec`) carries the byte budget + the
  three AXI shapes — worst-case a ripgrep-shaped result that truncates with a stated size,
  says "none" explicitly, and names the next file. Loads behind a dynamic import; proven out
  of the entry chunk by `bundle.spec.ts`. New E2E: `ui/e2e/search.spec.ts`.

These two are **separable** — Monaco enrichment can land with V7a, content search as V7b —
and content search is the one visual-track PR that also ships a server surface and an agent
tool, so it carries the full server test story.

### V8 — Settings UI

- **Owns:** a registered capability `ui/src/panels/Settings.tsx` + `ui/src/settings.ts` +
  one line in `tools.ts`; server `models/settings.py`, `services/settings.py`,
  `routers/settings.py`. Tests: `ui/e2e/settings.spec.ts`, `server/tests/test_settings.py`.
- **Builds:** the in-app surface for the knobs that today are environment variables — theme,
  Office native on/off/auto, voice on/off, telemetry stance (which is *off*, and the UI
  says so rather than offering it). It reads and writes **workspace/app-data settings only**,
  never `~/.claude` (the security posture from `CLAUDE.md`), and every setting is a typed
  field. Loads behind a dynamic import.
- **CI-verifiable:** fully.

### V9 — The custom title bar + native chrome tint (OWNER-GATED on the visual direction)

`DESIGN.md` and the ROADMAP freeze this into the visual bullet on purpose: the Feel track
already **tinted** the native caption from our tokens (`ui/src/captionTint.ts`, the cheap
90% — Windows still draws it, so drag/snap/maximise/buttons are unreimplemented). Replacing
it with a **fully custom frame** (our own drag region, our own buttons) is deferred here
because *what the strip carries is a consequence of the chosen visual direction* — the tape
belongs in it under one direction, pane identifiers under another. Built before that choice,
it gets built twice.

- **Owns:** `ui/src/captionTint.ts` (browser-safe seam, already exists) and — the one piece
  of ANVIL outside `ui/` — the native chrome tint that carries
  `desktop/src-tauri/src/host/class.rs`'s `PANEL_SURFACE`. **That `desktop/**` half is the
  desktop/office_host lane's to write, not this one's** — this plan names it and hands it
  the token contract; it edits no Rust.
- **OWNER-GATED:** it does not start until the owner ratifies the visual direction the rest
  of §1 establishes. Sequenced **last** in the visual track. The tinted caption is the
  shipping state until then; nothing regresses if V9 never lands.

---

## 2. First-run experience — the connect walkthrough

The find-what-exists half of first-run already shipped (M5 item 17: the welcome card + the
complete keyboard reference, `ui/src/panels/Keyboard.tsx`, §6.13, generated from the
registry) and the workspace-picker half shipped too (M5 item 5: the app says which folder
it is showing and whether anyone chose it). **What M7 owes is the *connect* half** — the two
walkthroughs the ROADMAP explicitly leaves open: **Claude login** and **Office/OnlyOffice
detection** — so a stranger on a fresh machine is told, honestly and once, what is and is
not wired up.

This is **a new registered capability**, `Setup` — its own module plus one line in
`tools.ts` — that composes with the welcome card rather than replacing it: the welcome card
teaches *the window* (panes, QuickBar, tools); Setup teaches *the connections* (is Claude
logged in, is Office hostable). Same discovery doctrine (§6.13): it interrupts nothing, it
is a panel not a modal, its affordances are real, and dismissal is workspace state.

### Server: an honest status, typed

`models/setup.py`, `services/setup.py`, `routers/setup.py` (thin: `GET /api/setup/status`):

```
class SetupCheck(BaseModel):
    id: Literal["claude_login", "office", "onlyoffice", "workspace", "shell"]
    state: Literal["ok", "action_needed", "unavailable"]
    detail: str            # one line the human reads, e.g. "Signed in as …" / "Run: claude /login"
    action: SetupAction | None   # a registered command id to run, or an external instruction

class SetupStatus(BaseModel):
    checks: list[SetupCheck]
    first_run: bool        # no .workbench/ state yet — drives auto-open
    all_ok: bool           # derived; the walkthrough gets out of the way when true
```

- **Claude login** is *detected, never performed* — the app cannot log a user in (that is
  the CLI's `claude /login`, and a credential action the safety rules keep out of the app).
  The check reports signed-in / not, and its `action` is an **instruction with the exact
  command**, not a button that runs a login.
- **Office / OnlyOffice** reuse the existing `GET /api/office/capabilities` (M4) — Setup
  *reads* that authority, it does not compute a second one. `office` reports whether native
  hosting is available and why not (no shell, no Office, hosting off — the honest-degradation
  string already exists); `onlyoffice` reports whether the fallback URL/JWT is configured.
- **`first_run`** is `no .workbench/ state` — the same signal the welcome card's auto-open
  uses. Setup auto-opens on first run *beside* the welcome card, and both retire once
  answered.

### UI: one registered capability, fake-first, CI-tested

`ui/src/panels/Setup.tsx` + `ui/src/setup.ts` + one line in `tools.ts`; mirror types in
`ui/src/types.ts`. It renders the `SetupStatus` as a checklist (each check a §6.4 status
pill + its detail + its one action), a QuickBar command ("Show setup…"), and a status-bar
reading that **hides when `all_ok`** (the quiet-bar rule). No `Alt` chord (the Scratchpad
precedent). Loads behind a dynamic import — it is not needed to paint. Dismissal is
`.workbench/setup.json`, beside `welcome.json` and `layouts.json`.

- **Fake-first / CI:** the whole first-run state is CI-drivable because `SetupStatus` is
  server-computed and every input has a fake: a workspace with no `.workbench/` is
  `first_run`; `WORKBENCH_OFFICE_FAKE` / capabilities drive the Office checks; the Claude
  check has a fake "signed out" state for the e2e. New journey `ui/e2e/firstrun.spec.ts`
  drives a fresh workspace and asserts: the walkthrough auto-opens, each check renders its
  honest state and its real action, answering/dismissing writes `setup.json`, and a
  second launch does **not** re-nag.
- **Owner's hands:** only the *real* Claude-login and *real* Office detection on a genuine
  fresh machine — the ten-minute exit-criterion walkthrough. The wiring is green on CI
  alone.

---

## 3. Voice input — the seam, the privacy posture, and what needs a mic

Voice is "an optional extra" (ROADMAP): local faster-whisper, push-to-talk, a domain
vocabulary in the initial prompt. The design work here is mostly **where the seam is** and
**what the privacy posture forces**, because the obvious implementation is the wrong one.

### The privacy decision, stated up front

The browser's `SpeechRecognition` API is the cheap path and it is **rejected as the
default**: in Chrome/WebView2 it streams the microphone to a Google cloud service. That
breaks the milestone's own **zero-telemetry, local-first** stance (and the north star's
local-first promise) — audio leaving the machine is exactly what the README will claim never
happens. So:

- **Default and only on-by-default path: local `faster-whisper`.** Audio is transcribed
  on-device; nothing leaves the machine. This is the privacy-correct posture and the one
  the README can stand behind.
- **Browser `SpeechRecognition` is a labelled, consent-gated fallback**, never silent: if
  offered at all, it is behind an explicit "this sends audio to your browser vendor's
  servers" consent (the safety-rule posture — nothing leaves the machine without consent),
  surfaced in the Settings UI (§V8), default off.

`faster-whisper` is a **new runtime dependency and a model download** — the model is large,
so the download is **owner-gated and consent-gated** (it is not shipped in the bundle by
default), and the dependency is justified in the PR body per the house rule. The dependency
being present must not force the model's presence: a machine with the package but no model
reports `action_needed` in Setup (§2), never a silent failure.

### The seam: a backend protocol with a fake, mirroring the Office host

The whole point is that the *wiring* is testable without a microphone. Model it exactly like
`HostBackend` / `FakeDocumentBridge`:

```
class VoiceBackend(Protocol):
    async def start(self, session: VoiceSession) -> None: ...
    async def stop(self, session_id: str) -> VoiceTranscript: ...   # push-to-talk: press, speak, release
```

- `models/voice.py`: `VoiceSession`, `VoiceTranscript` (`text`, `confidence`,
  `duration_s`), `VoiceCapabilities` (is a local model present? is the browser API
  offered?). `services/voice.py` holds `LocalWhisperBackend` and **`FakeVoiceBackend`
  (`WORKBENCH_VOICE_FAKE=1`)** returning a canned transcript. `routers/voice.py` thin:
  `POST /api/voice/start`, `POST /api/voice/stop`, `GET /api/voice/capabilities`.
- **Push-to-talk** is a UI gesture contributed to the Agent input (the focused agent pane,
  §6.11 — voice fills *the session you are talking to*), plus a QuickBar command and a chord
  the command earns. The domain vocabulary (MW/MWh, EUR/MWh, gate closure, day-ahead, the
  asset names) rides as an **initial prompt / bias to the local model**, so the transcriber
  gets the electricity terms right — a domain-aware touch, not generic dictation.
- The UI capability is `ui/src/panels/` contribution or an Agent-descriptor addition
  (a mic affordance is a card-on-input, not a panel — the annotate-mode precedent, M5 item
  3 PR 3), plus `ui/src/voice.ts`. Loads behind a dynamic import — it is not needed to paint.

### What is CI-testable vs what needs the owner

- **Fully CI-verifiable (no mic):** the whole seam through `FakeVoiceBackend` — start/stop
  lifecycle, the transcript flowing into the focused agent input, the push-to-talk gesture,
  the capabilities honesty (no model → `action_needed`), the consent gate on the browser
  fallback. New journey `ui/e2e/voice.spec.ts` drives `WORKBENCH_VOICE_FAKE=1` and asserts a
  canned transcript lands in the right pane's input and nowhere else.
- **NEEDS THE OWNER (a real mic + the model):** the actual `faster-whisper` transcription
  quality, the domain-vocabulary bias tuning, the model download UX, and the latency of
  push-to-talk on the owner's machine. These are **flagged for the owner** and are the one
  part of this milestone a headless CI runner cannot judge.

---

## 4. CI matrix + release — automatable vs owner-gated

The public repo wants a cross-OS CI matrix and a real release process building on the M4
packaging track (#87, the bundled backend + `tauri build`). The current CI
(`.github/workflows/ci.yml`) is **seven jobs** — `server`, `ui`, `e2e`, `perf` and
`desktop` on `windows-latest`; `changes` and `quality-gate` already on `ubuntu-latest` —
so the compute-heavy five are Windows-bound because the PTY is Windows
(`services/pty_manager.py`, pywinpty) and the Office host is Windows. Two of the three OSes
cannot even run those five today.

### C1 — Cross-platform PTY (the prerequisite, server) · CI-verifiable

The 3-OS matrix is blocked on one thing: `services/pty_manager.py` + `terminal_stream.py`
are pywinpty-only. This PR adds a **PTY backend seam** — the Windows path stays pywinpty; a
POSIX path uses the stdlib `pty` module — behind one protocol, chosen at construction, with
the existing terminal tests parametrised across whichever backend the OS provides.

- **Owns:** `server/src/workbench_server/services/pty_manager.py` (refactor to a backend
  protocol), a new `services/pty_posix.py`, and `server/tests/test_pty_manager.py`. No wire
  types change — a terminal is a terminal — so `ui/` is untouched.
- **CI-verifiable:** fully. On Linux/macOS the POSIX backend runs a real shell under the
  stdlib `pty`; on Windows pywinpty as today. This is the PR that *unlocks* the matrix.

### C2 — The 3-OS CI matrix · CI-verifiable

- **Owns:** `.github/workflows/ci.yml` only. Turns the `server`, `ui`, and (where possible)
  `e2e` jobs into a `strategy.matrix.os: [windows-latest, ubuntu-latest, macos-latest]`.
- **Honest scoping, written into the workflow:** the **server** and **ui** jobs go
  cross-OS immediately (once C1 lands). The **desktop** (Tauri/Rust + Office host window
  tests) and **Office-host e2e** jobs **stay Windows-only** — they test native window
  hosting that only exists on Windows, and saying so in a comment is the honest matrix, not
  a green tick faked by skipping. The **perf** lane stays on one pinned OS (the fixture
  numbers are only comparable on a fixed runner — `CLAUDE.md`). The quality-gate job gates
  on the matrix legs that apply per-OS.

### C3 — CONTRIBUTING, templates, zero-telemetry README · CI-verifiable (docs)

- **Owns:** `CONTRIBUTING.md`, `.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md`,
  and the README's zero-telemetry stance section. Documents the gate commands, the
  worktree-only-writes rule, and the local-first / nothing-leaves-the-machine posture that
  §3's voice decision and the absence of any telemetry make true. Docs-only; trivially green.

### C4 — Versioned Tauri releases with installers · **OWNER-GATED**

Builds on #87 (the bundled backend + `tauri build`). The *build* is automatable; the parts
that make it a **public** release are the owner's:

- **Automatable (CI):** a release workflow (`.github/workflows/release.yml`) that runs
  `tauri build` on a tag, produces the installer, and attaches it to a GitHub Release —
  **unsigned**, on a pre-release tag, to prove the pipeline. `tauri.conf.json` is already
  `"targets": "all"`, version `0.1.0`.
- **OWNER-GATED, and marked so — do NOT invent any of these:**
  - **Signing certificates** — code-signing on Windows (and notarisation on macOS if that
    OS ships) needs the owner's real certificate and secrets. CI cannot conjure one.
  - **The product NAME** — `tauri.conf.json` `productName` is the placeholder `"Workbench"`
    and the identifier `dev.workbench.app`. The real name is the owner's decision; **this
    plan does not propose one.** Every surface that hard-codes "Workbench" (the window
    title, the README, the bundle id) is enumerated in the PR body as the rename checklist,
    *left for the owner to fill*.
  - **Versioning policy** — when `0.1.0` becomes `1.0.0`, and the version bump ritual.
  - **The public-launch decision itself** — flipping the repo public, the announcement.

---

## 5. The PR sequence — CI-buildable vs owner-gated

Ordered by dependency. The visual track (§1) is largely parallel — its PRs own disjoint CSS
and can land in any order after **V1** (the frame everything sits in) — while the capability
and release tracks have real edges. **Fake-first / CI-verifiable PRs land first and prove
the milestone on a headless runner; owner-gated PRs are separated out explicitly and never
block the CI-buildable core.**

### Wave A — foundations (parallel; CI-verifiable)

1. **V1** (dock/tab/pane chrome) — the visual frame; other visual PRs inherit it.
2. **C1** (cross-platform PTY) — unblocks the CI matrix; server-only, touches no UI.

Disjoint: V1 is `ui/src/styles/dockview.css` + `panes.css`; C1 is
`services/pty_manager.py`. No overlap.

### Wave B — surfaces + connections (parallel once V1 lands; CI-verifiable)

3. **V2** empty states + welcome surface · **V3** QuickBar · **V4** status bar + toasts
   (V4 rebases onto V3 for `overlays.css`) · **V5** agent/chat · **V6** document mat.
   Each owns disjoint CSS + component class names (§1); they run as separate lanes.
4. **Section 2 — First-run / Setup** (`models/setup.py` + `services/setup.py` +
   `routers/setup.py` + `ui/src/panels/Setup.tsx` + `setup.ts`). Composes with the welcome
   card; reads `office/capabilities`. Fully CI-drivable via `firstrun.spec.ts`.
5. **C2** (3-OS matrix) — `.github/workflows/ci.yml`, after C1.
6. **C3** (CONTRIBUTING/templates/README) — docs; anytime.

### Wave C — capabilities that dress the editor (CI-verifiable)

7. **V7a** Monaco ANVIL theme · **V7b** content search (server + UI + agent tool) ·
   **V8** Settings UI. Each is a registered capability behind a dynamic import, proven out
   of the entry chunk by `bundle.spec.ts`. V8 (Settings) is where §3's voice on/off and the
   telemetry stance surface, so it lands **before or with** the voice PR.

### Wave D — voice (seam CI-verifiable; quality owner-gated)

8. **Section 3 — Voice seam** (`models/voice.py` + `services/voice.py` with
   `FakeVoiceBackend` + `routers/voice.py` + `ui/src/voice.ts` + the push-to-talk gesture).
   The **wiring is CI-green** via `WORKBENCH_VOICE_FAKE=1` + `voice.spec.ts`; the **real
   `faster-whisper` model, mic quality, and domain-vocabulary tuning are OWNER-GATED** and
   flagged as the one part CI cannot judge.

### Wave E — owner-gated finish (do NOT land without the owner)

9. **V9** — the fully custom title bar + native chrome tint. **OWNER-GATED on the visual
   direction** ratified across §1; the `desktop/**` half belongs to the desktop/office_host
   lane. The tinted caption is the shipping state until then.
10. **C4** — signed, versioned Tauri release. The **unsigned pre-release pipeline is
    CI-automatable**; **signing certs, the product NAME, versioning policy, and the
    public-launch decision are OWNER-GATED** — this plan invents none of them.

### The clean split, stated once

- **Fully CI-verifiable, no owner, no special hardware:** V1–V8, Setup (§2), the voice
  *seam* (§3 via the fake backend), C1, C2, C3, and the unsigned release pipeline half of
  C4. This is the entire premium look, the first-run experience, content search, settings,
  the voice wiring, and a cross-OS matrix — all green on a headless runner.
- **Needs the owner's hands / real hardware / a decision:** the real Claude-login + Office
  detection on a genuine fresh machine (the ten-minute walkthrough), the real voice model +
  mic + vocabulary tuning, the custom title bar's visual direction (V9), and every part of
  the public release that is a signature, a name, a version policy, or the launch itself
  (C4). Each is marked **OWNER-GATED** where it appears above, and **no product name is
  proposed anywhere in this plan.**

## Exit criterion (from the ROADMAP)

A stranger on a fresh Windows machine reaches a **working, secured, distinctive** product in
under ten minutes: the app looks like ANVIL and nothing else (V1–V8), the first-run
walkthrough tells them honestly what is connected (§2), voice is there if they want it and
never ships their audio anywhere without consent (§3), and there is a real, versioned,
installable build to hand them (C4) — the CI-buildable core provable today, the owner-gated
finish the last mile only the owner can walk.

## What this plan deliberately defers (so the scope is honest)

- **A product name.** Owner-gated; not proposed. `tauri.conf.json` keeps `"Workbench"` /
  `dev.workbench.app` as placeholders and C4 enumerates the rename checklist for the owner.
- **Code-signing / notarisation.** Needs the owner's certificate; the CI pipeline proves
  itself unsigned first.
- **The real voice model.** `faster-whisper` + its (large, consent-gated) model download is
  owner-gated; the seam ships and is CI-green with the fake backend.
- **The custom title bar (V9).** Waits on the ratified visual direction, by the ROADMAP's
  own scope freeze; the tinted caption is the shipping state.
- **Floating / popped-out panels** (M5 item 13) — unclaimed and unrelated to M7's identity
  work; not pulled forward here.
- **The public-launch decision** — flipping the repo public and announcing — is the owner's
  alone.

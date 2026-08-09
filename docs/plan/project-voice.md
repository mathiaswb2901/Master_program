# Project Voice — the agent learns how *you* write *this* project

Status: **plan** (this document is the capability's Plan PR; no feature code lands
with it). Owner-ratified 2026-08-09 evening. It turns one owner sentence — *"learn how
I write THIS project, prove what you learned, stay local"* — into a sequence of disjoint,
fake-first, independently shippable PRs with explicit file ownership, so parallel lanes
never collide. It is written against the repo as it stands after M6/loops (the registry,
panes, the Office host with its fake-first document bridge, the reconciliation gate, the
watcher bus, per-workspace `.workbench/` data, the bundled skills plugin) and **composes
with those seams rather than duplicating them**.

## The value sentence

**The agent writes like *you* on *this* project, and proves each thing it learned.**
Every line of the style profile cites a count or a verbatim example you can check,
correct, or delete; the profile lives in *this* workspace, so switching projects
switches the voice — because you do not write a grant memo the way you write a code
review, and neither does the agent helping you. No other agent workspace persists a
per-project style model: Claude-for-Word has one global assistant with no memory of how
*you* write *this* document. That is the differentiator, and it is only a differentiator
if it is **accurate** — a profile that invents a trait you do not have is worse than no
profile, so every trait ships with its receipt and every receipt is falsifiable.

## The honest posture, stated once

Three commitments frame every decision below, because getting them wrong turns a
differentiator into a liability:

1. **Observable and verifiable only.** The profile records things that can be *counted*
   or *quoted* — average sentence length, Oxford-comma yes/no, `(Author, Year)` vs `[n]`,
   the hedges you actually use — never an unfalsifiable "sophisticated, authoritative
   tone" the user cannot check. Every trait carries an evidence receipt (a count with its
   denominator, or short verbatim excerpts) so trust is earned per line, not asked for.
2. **The written artifact, not the keystroke.** v1 learns from what you *saved* — the
   `.docx` on disk — re-sampled on the watcher's save event, debounced, and on demand.
   Real-time "watch me type" keystroke capture is named as an **owner-gated** follow-up
   (the same posture as the real voice-dictation backend), not smuggled into v1.
3. **Local, and yours.** The profile is derived from prose the agent *already* reads when
   it opens the document; it is stored in your workspace under `.workbench/style/`, never
   global, never `~/.claude`, and never leaves the machine. You can read it, edit it, or
   delete it at any time — it is your data, in your project.

## Design constraints carried from the house rules

Every PR below inherits these without restating them:

- **Typed payloads only.** Every REST/WS body is a Pydantic model in
  `server/src/workbench_server/models/`, mirrored in `ui/src/types.ts`. `mypy --strict`,
  ruff, pytest green; new behaviour ships with tests. structlog only, never `print`;
  thin routers, logic in `services/`; `pathlib` everywhere; DESIGN.md tokens, zero raw hex.
- **Registered capability.** The UI surface is one module (`ui/src/panels/StylePanel.tsx`
  + `ui/src/style.ts`) plus one line in `ui/src/tools.ts` — never an edit to `App.tsx`,
  `commands.ts` or `StatusBar.tsx`.
- **Plural by default, no singleton.** A profile is *attached to a workspace root*, looked
  up through the store that owns that root; nothing is `store.activeStyleProfile`. Two
  Style review panes over the same profile are views, not owners.
- **Disk file discipline.** The profile store follows the exact contract
  `services/layouts.py` and `services/settings.py` already established: version-stamped,
  atomic write (`tmp` + `os.replace`, retried past a transient Windows lock),
  `utf-8-sig` on read, and **a read that never raises** — a corrupt or stale profile costs
  the user their learned voice and nothing else, resolving to "empty + a sentence saying why".
- **Agent-tool byte budgets.** Nothing here adds a chat-session agent tool with a large
  schema (see §4 — the apply seam is a bundled skill reading a plain file, not a new tool),
  so the per-session schema cost the `AgentToolSpec` budget guards is untouched.

---

## 1. SCOPE — per workspace, in `.workbench/style/`

The profile lives at **`<workspace>/.workbench/style/`**, and that placement *is* the
owner's "everyone writes differently per project". The precedent is exact and already
load-bearing:

- `services/layouts.py` keeps `.workbench/layouts.json` **in the workspace**, "so different
  projects keep different windows"; the `remember` skill keeps `.workbench/memory.md` in the
  workspace, "durable facts about *this workspace*". A style profile is the same shape of
  fact — a property of the project, not of the person — so it lives beside them.
- It is **never** in the machine's app-data dir (`services/app_data.py`) and **never** in
  `~/.claude`. App-data is for things about the person at the keyboard (the theme, recent
  workspaces); `~/.claude` carries the global hooks and permission rules the server
  deliberately keeps out of sessions (`CLAUDE.md`, `settings.py`'s note). A voice that
  followed you across projects would be the bug, not the feature.
- `.workbench/` is **already gitignored** (`.gitignore` line 33), so the profile is not
  committed unless the user chooses to — the same "your own data" status as `memory.md`
  and `shortcuts.md`.

**Switching projects switches the profile — and this is a wiring obligation, not a hope.**
`.workbench/style/` is rooted in the workspace, so the store copies `workspace.root` into a
path of its own and therefore **owes a `set_workspace_root`** and **must be listed in
`create_app`'s `WorkspaceService` rootables** (`services/workspaces.py` holds "the one list
of everything that re-roots"). The house rule is blunt about the failure mode: "a service
that copies `workspace.root` into a field of its own must implement `set_workspace_root`
**and** be listed … — one that is not keeps serving the folder the user left." PR 1's tests
assert exactly this: switch the root, and `GET /api/style` answers with the *new* project's
profile (or its empty state), never the old one. This is the owner's central requirement,
made mechanical.

## 2. OBSERVE — sample the saved artifact, on the watcher's save event

### The mismatch, corrected loudly

The task says "sample the user's prose via the existing document bridge read path
(#65/#78/#92)". Grounding that against the real code surfaces a mismatch that must be
stated plainly rather than papered over:

> **The `DocumentBridge` (`services/office_host/document_bridge.py`, #65/#78/#92) reads the
> *live, docked* document** — the Word instance Workbench reparented into a panel, including
> the user's unsaved on-screen edits. That path requires the **desktop shell + installed
> Office + a document the user has open right now**. It is not available in CI, it is not
> driven by a save, and it cannot see a document the user closed. The "honest v1" the owner
> asked for learns from the **written artifact**, re-sampled on save — which is a *disk*
> read, not a live-COM read.

This is the **same decision the reconciliation gate already made and documented** (see
`docs/plan/m6-proof.md` §2 and `services/reconciliation.py`): the workbook↔code gate reads
the `.xlsx` **directly with openpyxl**, deterministically, on the CI machine, and treats the
live-COM bridge (reconciling an *open, unsaved* workbook) as the optional *later* path behind
the same reader protocol. Project Voice mirrors that split one-for-one:

- **v1 reads the saved `.docx` from disk** with a pure-Python reader, deterministic and
  CI-verifiable with no Office. The bridge's `WordText` / `read_word` shape — a paragraph
  stream, windowed — is exactly what a Word document *is* to this system, so the disk reader
  produces the same paragraph-stream shape and the two are interchangeable behind one seam.
- **The live-COM reader is the named later path.** Reading the *unsaved on-screen prose* of a
  docked Word through `DocumentBridge.read_word` — "watch me write" as it happens in the
  hosted editor — slots in behind the same `ProseSource` protocol, mirroring reconciliation's
  `LiveComWorkbookReader`. It needs the owner's Office box for its manual verification and is
  deferred out of v1 (§7).

So "read what the document bridge exposes" is honoured as **the shape and the later live
path**; v1's transport is the disk, exactly as the moat's first domain gate ships openpyxl
before COM. This correction is the plan's single most important grounding decision.

### The reader seam (mirrors the office-host reader/fake split)

`services/style/prose_source.py` — a small `Protocol` with two methods and two
implementations, the `HostBackend` / `FakeDocumentBridge` pattern:

```
class ProseDocument(BaseModel):
    source: str            # workspace-relative path
    paragraphs: list[str]  # the body as a paragraph stream (the WordText shape)
    heading_levels: list[int | None]   # per-paragraph outline level, None for body text

class ProseSource(Protocol):
    def kinds(self) -> set[str]: ...                     # {".docx"} for v1
    async def read_prose(self, path: Path) -> ProseDocument: ...
```

- **`DocxProseReader`** (v1, CI): reads `.docx` from disk with **python-docx**, paragraph
  text + outline level, read-only. This is the one new runtime dependency (§5), justified
  exactly as openpyxl was: the standard pure-Python OOXML reader, no native build, and
  "hand-parsing the OOXML zip with the stdlib is reinventing python-docx for no gain".
- **`LiveComProseReader`** (later, owner-gated): the same shape from
  `DocumentBridge.read_word` against a docked instance — deferred (§7).

### The learning hook (mirrors the reconcile-on-save precedent)

`services/style/learner.py` — a `StyleLearner` that **subscribes to the event bus** and
re-samples on save, the exact mechanism `services/reconcile_spec.py` already uses for
workbooks:

- On a `FileChangedEvent` (`services/watcher.py`) with `change in {"added", "modified"}`,
  suffix in `ProseSource.kinds()`, and not a temp/lock file, it **debounces**
  (`WORKBENCH_STYLE_DEBOUNCE_S`, the `WORKBENCH_RECONCILE_DEBOUNCE_S` precedent) and re-runs
  the extractor over that document, folds the result into the profile, persists it, and
  publishes a `StyleProfileEvent` on `/ws/events` so the review panel updates live.
- **On demand**: `POST /api/style/resample` re-samples the whole corpus (the newest N
  `.docx`, bounded — see §3's truncation).
- **Bounded, in code, not prompted.** A per-run wall-clock/size ceiling
  (`WORKBENCH_STYLE_*`, the reconcile precedent), and a cap on how many documents and how
  many characters per document are sampled, so a 400-file project cannot wedge the learner.
  A capped corpus is *stated* in the profile (§3), never silent.

A save of a `.docx` already produces a `FileChangedEvent` on the bus today — the watcher
hashes and publishes it — so the hook needs **no new watcher signal**; it is a subscriber,
like provenance and activity.

## 3. EXTRACT — observable, verifiable features, each with a receipt

The extractor (`services/style/extractor.py`) is a set of **small pure functions**, one per
feature, each turning a paragraph stream into a `StyleTrait` carrying its evidence. No heavy
NLP dependency: sentence/word segmentation and the pattern detectors are stdlib `re`
heuristics, and where a heuristic is imperfect (passive voice is the honest example) the
trait is reported **conservatively with example sentences the user verifies** — the receipt
is what makes a heuristic safe to show.

The v1 feature set — every one countable or quotable:

| Feature (`kind`) | What it records | The receipt |
|---|---|---|
| `sentence_length` | mean words/sentence, and spread | count over N sentences |
| `voice_lean` | active vs passive lean | % passive + up to 3 example sentences flagged |
| `person` | first-person `we` vs `I` (vs impersonal) | counts of each |
| `oxford_comma` | uses it / omits it | count of series seen with vs without |
| `citation_format` | `(Author, Year)` vs `[n]` vs none | detected pattern + example matches |
| `hedging` | the hedges you actually use | the hedge words found + per-word counts |
| `headings` | outline depth, numbered vs not, sentence-case vs Title-Case | counts from the outline levels |
| `term_preference` | consistent spelling/capitalisation choices | the variant chosen + its count vs the alternative |

**Confidence is derived from sample size, and stated — never asserted.** An Oxford-comma
verdict from 2 series is `low`; from 40 series it is `high`. A trait with too little evidence
is reported as low-confidence *and says so*, or is withheld entirely — this is the mechanical
guard against inventing a trait: the profile literally cannot claim `high` confidence it did
not measure. "Sophisticated tone", "confident voice", any adjective the user cannot check
against a count or a quote, is **out of scope by construction** — there is no `StyleTrait`
kind that can carry one.

### The profile is human-readable and user-editable — and this is a real design point

Two representations, one source of truth, chosen to satisfy both "the machine keeps it fresh
on every save" and "it is your data, edit or delete it":

- **`.workbench/style/profile.json`** — the typed `StyleProfile`, the machine's source of
  truth, under the version-stamped / atomic-write / read-never-raises disk discipline above.
- **`.workbench/style/profile.md`** — a rendered, human-readable view the learner writes
  beside it on every update, so the user can read and diff the profile with plain tools
  (the `memory.md` reading experience). Each trait is one line: *key — value — evidence*.

**User edits are respected via a `held` flag, so re-sampling never clobbers a correction.**
When the user corrects or pins a trait (in the UI, PR 2, or by hand-editing the JSON), that
trait is marked `held`; the next save-triggered re-sample still *measures* it and records the
fresh measurement in `measured`, but does not overwrite the held `value`. The panel surfaces
the divergence ("you set *omits Oxford comma*; the last 30 saves measure *uses it* — keep or
update?"). This is what makes the profile *trustworthy under continuous learning* rather than
a thing that silently reverts the user's own correction on the next save — the single
behaviour that separates an accurate profile from an annoying one. Deleting the trait, or the
whole `.workbench/style/` folder, is always available and always final.

## 4. APPLY — how a writing agent in this workspace reads the profile before drafting

The task asks to weigh a **bundled skill** against a **context seam**, against how the skills
bundle actually loads per session, and pick. The mechanics, from the real code:

- **The bundled skills plugin** (`services/skills_bundle.py`, `services/sdk_factory.py`) is
  passed per session as `--plugin-dir`, namespaced `workbench:<name>`, **session-scoped**
  (nothing written to `~/.claude`), degrading to no-skills if absent. Two skills — `remember`
  and `plan-visual` — carry a narrow auto-allow rule (`_AUTO_ALLOWED_SKILLS =
  [Skill(workbench:plan-visual), Skill(workbench:remember)]`) so the agent may invoke them
  with no permission prompt. `plan-visual` is *additionally* named in the system-prompt append
  (`_system_append`); `remember` instead relies on its own description to get reached for.
  Either lever makes a skill reliable, and `remember` reads `.workbench/memory.md` — the
  *exact* precedent for a skill that reads `.workbench/style/`.
- **The context seam** is `build_context_bridge` (the `get_workspace_state` MCP tool) and
  `_system_append` (the sentence every session is told). Injecting the *whole* profile into
  the system prompt would put it in front of every session — including code-only sessions
  that never write prose — and pay its tokens on **every request**, which is precisely the
  cost the `AgentToolSpec` schema budget and the "office tools not in every chat session"
  decision exist to avoid.

**Pick: a bundled `write-like-me` skill that reads `.workbench/style/profile.md` on demand**,
added to `_AUTO_ALLOWED_SKILLS` and named in the writing-session `_system_append` so a
drafting agent reaches for it before writing prose. Why this over the alternatives:

- **Pay-per-use, not pay-per-request.** The profile's tokens are spent only when a session
  actually drafts and reads it, not on every code session — the AXI/ergonomics discipline.
- **No new agent tool, no new schema byte.** The skill uses the agent's existing `Read` on a
  file already inside the workspace jail; a bespoke `read_style_profile` context-bridge tool
  would be reinventing `Read` and widening every session's schema cost for nothing.
- **The security posture is inherited whole.** Session-scoped, `workbench:`-namespaced,
  nothing in `~/.claude`, degrades gracefully — the `remember` guarantees, unchanged.

The one shared-file touch is two append-only lines in `sdk_factory.py` (the `_AUTO_ALLOWED_
SKILLS` entry and the writing-session append), the same edit `plan-visual` and `remember`
made — sequenced and called out in PR 2 rather than rediscovered, exactly as `m6-proof.md`
flagged the `mission.ts` touch. The skill's `SKILL.md` follows `remember`'s doctrine: read
the profile first, treat it as guidance the user can override, and never fabricate a trait
the file does not contain.

The UI review/edit surface (PR 2) is the other half of "apply": it is where the user *sees*
what was learned and its receipts, accepts or corrects a trait (`held`), and deletes the
profile — the trust loop that makes the agent's use of it legitimate.

## 5. Models added (`server/src/workbench_server/models/style.py`)

```
StyleFeatureKind = Literal[
    "sentence_length", "voice_lean", "person", "oxford_comma",
    "citation_format", "hedging", "headings", "term_preference",
]
Confidence = Literal["low", "medium", "high"]

class TraitEvidence(BaseModel):
    count: int | None = None        # e.g. 27 passive constructions
    of_total: int | None = None     # …of 310 sentences — the denominator, so it is a rate
    examples: list[str] = []        # short verbatim excerpts, each length-bounded

class StyleTrait(BaseModel):
    kind: StyleFeatureKind
    key: str                        # stable id, e.g. "oxford_comma"
    value: str                      # human value: "uses the Oxford comma"
    evidence: TraitEvidence         # the receipt — a count and/or quotes
    confidence: Confidence          # derived from sample size, never asserted
    held: bool = False              # user pinned this value; re-sample won't overwrite it
    measured: str | None = None     # the latest measurement when it diverges from a held value

class StyleSample(BaseModel):       # provenance: what prose this was learned from
    source: str                     # workspace-relative .docx path
    paragraphs: int
    chars: int
    sampled_at: datetime

class StyleProfile(BaseModel):
    version: int
    workspace_label: str            # which project this voice belongs to
    traits: list[StyleTrait]
    samples: list[StyleSample]
    updated_at: datetime
    truncated: StyleTruncation | None = None   # AXI shape 1: corpus capped + how to widen

class StyleProfileState(BaseModel): # GET /api/style
    profile: StyleProfile | None    # None + reason when nothing learned yet — "none" said out loud
    problem: str | None = None      # the settings.py "empty + why" idiom

class StyleProfileEvent(BaseModel): # on /ws/events — the one shared-union addition
    type: Literal["style_profile"] = "style_profile"
    state: StyleProfileState
```

`StyleProfileState.profile is None` is the explicit **"nothing learned yet"** (AXI shape 2 —
say "none", do not return blankness); `truncated` states a capped corpus and names the
`scope`/`limit` that widens it (shape 1). Mirrored in `ui/src/types.ts` for the review panel.
The `ProseDocument` / `ProseSource` seam values are in-process only (not on the wire), like the
office-bridge read models — modelled anyway so the fake and the later live reader must produce
the same shape.

---

## 6. The PR sequence

Two PRs, each fake-first and independently shippable, with **disjoint file ownership** so
parallel lanes never collide. PR 1 is the only hard prerequisite; PR 2 depends on it.

### PR 1 — the profile store, the extractor, and the save-triggered learning hook (server) · foundation

- **Owns:** `server/src/workbench_server/models/style.py`;
  `server/src/workbench_server/services/style/` (`__init__.py`, `prose_source.py` with
  `DocxProseReader` + a `FakeProseReader`, `extractor.py`, `store.py`, `learner.py`);
  `server/src/workbench_server/routers/style.py`
  (`GET /api/style`, `POST /api/style/resample`, `PUT /api/style` for a held edit,
  `DELETE /api/style`); its registration and `WorkspaceService`-rootable listing in
  `create_app`; the `python-docx` dependency; and its tests
  (`server/tests/test_style.py`, `server/tests/style_fixtures.py`).
  The **one shared file** it touches is the event-bus typed union — a one-line
  `StyleProfileEvent` addition, exactly the `SessionActivityEvent` / `FileProvenanceEvent`
  precedent.
- **Builds:** the models; the `ProseSource` seam with the disk `.docx` reader; the eight pure
  extractor functions with derived confidence; the disk store (version-stamped, atomic,
  read-never-raises, JSON source of truth + rendered `profile.md`, the `held` merge); the
  bus-subscribed `StyleLearner` with debounce and bounds; the REST surface; and — the owner's
  core requirement — `set_workspace_root` on the store, listed in `create_app`'s rootables.
- **Fake-first / CI:** fully. `style_fixtures.py` builds tiny `.docx` files with python-docx
  (a heading-and-body sample, a passive-heavy sample, an Oxford-comma sample, a `(Author,
  Year)` sample). No Office, no COM, no desktop shell.
- **Test story (`test_style.py`):**
  - each extractor on a known paragraph stream → the exact count/example it should record
    (e.g. the Oxford-comma fixture yields `value="uses the Oxford comma"`, `count=N`);
  - confidence is `low` on a 2-series fixture and `high` on a 40-series fixture — the
    invented-trait guard is asserted, not asserted-about;
  - the store round-trips; a corrupt `profile.json` yields empty-state + a reason and never
    raises; a `held` trait survives a re-sample that measures the opposite (records `measured`,
    keeps `value`);
  - **the learning hook end-to-end**: publish a synthetic `FileChangedEvent` for a fixture
    `.docx` on the bus → after the debounce, the profile gains the fixture's trait and a
    `StyleProfileEvent` is published (the deterministic half of the E2E moment, with no UI);
  - **the scope guarantee**: `set_workspace_root` to a second fixture workspace → `GET
    /api/style` answers with the second project's profile (or its empty state), never the first.
- **Owner's hands / real Office:** **none.** Green on a machine with no Office installed —
  the whole point of the disk-reader-first design.

### PR 2 — the agent-application seam + the review/edit UI (skill + UI) · depends on PR 1

- **Owns:** the bundled skill
  `server/src/workbench_server/skills_bundle/skills/write-like-me/SKILL.md`; two append-only
  lines in `services/sdk_factory.py` (`_AUTO_ALLOWED_SKILLS` + the writing-session
  `_system_append`); the UI capability `ui/src/panels/StylePanel.tsx` + `ui/src/style.ts` +
  one line in `ui/src/tools.ts` + the mirror types in `ui/src/types.ts`; and tests
  (`server/tests/test_style_apply.py`, `ui/src/style.test.ts`, `ui/e2e/style.spec.ts`).
- **Builds:** the `write-like-me` skill (reads `.workbench/style/profile.md`, treats it as
  overridable guidance, never fabricates); its auto-allow + system-append wiring; and the
  **review/edit panel** — the evidence gallery (one row per trait: its value, its confidence
  as a §6.4 status pill, its receipt count/examples in tabular figures per the DESIGN.md
  numeric rule), an *accept/correct* control that sets `held` via `PUT /api/style`, a
  *resample now* command, a *delete profile* action, and a status-bar reading (traits learned,
  hides at zero). Zero raw hex; the confidence pill maps onto the existing semantic/agent-status
  ramp (`low → --info`, `medium → --warn`, `high → --success`), inventing no colour — the
  `m6-proof.md` risk-badge precedent. Plural-safe: two Style panes over one profile, asserted
  through a save/restore round trip (the plural-tool test every plural capability owes).
- **Fake-first / CI:** fully. vitest against typed fixtures; `test_style_apply.py` asserts the
  skill is present under `workbench:write-like-me`, is in `_AUTO_ALLOWED_SKILLS`, and is named
  by the writing-session append (the seam delivers the profile). The **E2E journey**
  (`ui/e2e/style.spec.ts`, `WORKBENCH_FAKE_AGENT=1`): seed a `.docx` in the workspace → save
  → the StylePanel gains a **verifiable new trait line with its receipt** → run a scripted
  fake-agent draft turn and assert the draft **reflects that trait** (the fake agent is
  scripted to echo the profile the seam handed it — proving the profile *reached* the drafting
  session). This is the owner's E2E moment, deterministic end to end.
- **Honest boundary of the E2E:** the journey proves the *seam* — the learned trait reaches a
  drafting session in this workspace. That the *real* Claude then writes measurably more like
  the user is a **qualitative claim the owner verifies by hand** with real prose and real
  Office, not a CI assertion. Stated here so the proof is not oversold.

### Sequencing and collisions

PR 2's only shared-file touch is the two append-only lines in `sdk_factory.py`; if a
concurrent lane is editing that file, PR 2 rebases onto it — the append is order-independent.
Everything else in both PRs is new files or a one-line registration (`tools.ts`, the bus
union), the disjoint-ownership pattern the whole modular track is built on.

---

## 7. The sibling ask — "smart orchestration: all agents have an overview of all files"

Assessed honestly, because the composability principle says two live-fleet views that
disagree is worse than one, and the answer is mostly "you already have most of this".

**What already exists, and what it gives an agent today:**

- **The orchestrator kind (#63, `services/orchestrator.py`)** — an orchestrator session
  spawns/lists/reads/sends/stops workers, each bound to a pooled worktree, and can *read a
  worker's transcript*. Mission Control renders the crew. So a captain already has an overview
  of what its crew is *doing*.
- **`workspace_search` (registered agent tool, `services/search.py`)** — any agent can search
  the whole workspace's *content*, bounded, respecting the ignore rules. So "what is in these
  files" is already a call away.
- **`get_workspace_state` (the context bridge)** — the *active/open/dirty* files, so an agent
  avoids editing a buffer with unsaved user changes. This is the closest existing thing to
  cross-agent file awareness, but it is **per-window UI state**, not the fleet's.
- **`services/activity.py`** ("what every session is touching *right now*") and
  **`services/provenance.py`** ("who wrote this file, after the fact"). Both are computed and
  bounded — but both are **UI-facing read models**, not something an agent can *query before it
  edits*.

**The real gap, named precisely:** there is no **agent-facing, path-keyed** answer to *"is
another agent already working on this file?"* An agent about to edit `dispatch.py` cannot ask
the fleet whether a sibling is mid-edit on it; activity and provenance know, but only the UI
reads them.

**Recommendation (small, composed, not over-scoped):** if this is pursued, it is *one*
context-bridge tool — call it `workspace_activity` — that surfaces the **already-computed**
activity+provenance **join, keyed by path** ("sessions touching X now; last writer of X"),
so an agent consults it before editing a contested file. This is a **join over existing
services** (the `ui/src/mission.ts` "compute the join and nothing else" precedent), not a new
authority, not a new store, and it honours the agent-tool byte budget (a compact text result,
the three AXI shapes). It composes with Project Voice for free: the same seam could later
answer "which style profile governs this workspace", but that is not needed for v1.

**Explicitly out of scope — do not build with this plan:** a full *shared-intent index* where
agents *declare* the file sets they intend to touch and a lock manager arbitrates — a genuine
cross-agent coordination layer. That is a separate, larger piece with its own failure modes
(stale intents, deadlock, an agent that declares and dies), and folding it into the
style-learning plan would be exactly the scope-creep the "recommend, do not over-scope"
instruction warns against. Named as its own future work, not smuggled in here.

---

## 8. What this plan deliberately defers (so the scope is honest)

- **Real-time keystroke capture ("watch me type").** v1 learns from the saved artifact.
  Live keystroke observation is **owner-gated**, the same posture as the real voice-dictation
  backend (`services/voice.py`'s `register_backend` seam): named, not built, no cloud path.
- **Live-COM sampling of unsaved on-screen prose.** Reading a docked Word's live paragraphs
  through `DocumentBridge.read_word` (the "watch me write in the hosted editor" path) is the
  named later reader behind the same `ProseSource` protocol — it needs the owner's Office box
  for verification, mirroring reconciliation's deferred live-COM reader.
- **Cross-document consistency enforcement.** Making the agent's drafts *consistent across
  many documents*, or flagging where the user's own prose drifts from their profile, is a
  second capability on top of the profile — deferred.
- **Multi-author disambiguation — whose prose is whose.** v1 assumes the workspace's `.docx`
  prose is the user's. A co-authored document blends voices, and a blended profile is an
  inaccurate one. This is a **hard limitation, stated on the profile** (the `samples` list
  names what it learned from, so the user can see a co-authored source and delete it), and true
  per-author attribution — tracked-changes authorship, revision metadata — is deferred.
- **Broadening the prose corpus** beyond `.docx` (Markdown, `.txt`, chat drafts). The
  `ProseSource.kinds()` seam makes a plain-text reader a small later addition; v1 scopes to
  `.docx` to compose with the Office moat and keep the first cut honest.
- **Persisting learning provenance beyond the profile itself.** Like provenance/activity/usage,
  nothing here writes a history log; the profile is the state, and a git history of
  `.workbench/style/` (if the user commits it) is the only timeline. A richer audit is later.

## 9. What needs real Office / owner hands vs. what is fully CI-verifiable

- **Fully CI-verifiable (no Office, no owner):** both PRs in their entirety — the disk-`.docx`
  extractor against committed fixtures, the save-triggered learning hook, the scope-switch
  guarantee, the store's failure modes, the apply seam's wiring, and the E2E *seam* proof under
  `WORKBENCH_FAKE_AGENT=1`. This is the whole point of the artifact-first / fake-first design:
  the differentiator is green on a machine with no Office installed.
- **Needs real Office + owner verification (deferred):** the live-COM prose reader, and the
  qualitative judgement that the real model, handed the profile, writes measurably more like
  the user on real prose — a human read, not a CI assertion. Both are named as *later* work so
  the shippable core is not blocked on them, exactly as the Office host sequence shipped its
  fake backend before its Rust and COM.

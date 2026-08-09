/**
 * The per-run temp workspace the E2E backend is launched in.
 *
 * Created once per `playwright test` run and seeded with everything the
 * journeys need: a folder to create files in, a text file the fake agent can
 * Read, an office document (for degraded mode), a `.workbench/shortcuts.md`
 * carrying one working shortcut and one malformed entry, the two folders
 * named `target` that journey 3 uses to prove build caches are skipped by their
 * `CACHEDIR.TAG` rather than by their name, and a `.claude-projects` store of
 * seeded conversations (journey 11) — one per case the browser has to tell
 * apart: inside the workspace, outside it, a folder that no longer exists, and
 * a transcript that will not parse.
 *
 * Why two env vars: `playwright.config.ts` is loaded by the runner *and* by
 * every worker process, so creating the directory unconditionally would give
 * each worker a different workspace. The runner seeds one and publishes it as
 * `WB_E2E_WORKSPACE_ACTIVE`, which workers inherit — so `E2E_WORKSPACE` is the
 * same directory everywhere, in the config, in the specs, and in the server.
 * That handoff is internal; nobody sets it by hand.
 *
 * `WB_E2E_WORKSPACE` is the public knob, and it means one thing: *seed this
 * run's workspace here* (instead of a fresh temp directory) so a failing
 * journey can be re-run against a path you chose. It must therefore name an
 * empty or nonexistent directory. A run against a workspace a previous run
 * left behind would hit the `src/bid.py` journey 1 expects to create, and the
 * seeded files journeys 3 and 4 expect to find as they were written — so a
 * non-empty one is refused up front rather than failing obscurely later.
 *
 * The directory is deliberately left behind: it holds the exact state a failing
 * journey left, next to the Playwright trace. So is its `-projects` sibling,
 * the seeded Claude transcript store (below) — the same reason, and the same
 * convention as the perf lane's fixture.
 */

import child_process from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/** Public: "seed this run's workspace here". Empty or nonexistent, or nothing. */
export const WORKSPACE_ENV = "WB_E2E_WORKSPACE";
/** Internal: the seeded path, runner -> workers. Set below, never by hand. */
const ACTIVE_ENV = "WB_E2E_WORKSPACE_ACTIVE";

/**
 * Prefix of the temp directory, and a constraint on every terminal marker in
 * the suite: a PowerShell prompt prints its CWD, so this string plus a random
 * suffix is on screen during terminal assertions. A marker that starts with
 * `e2e-` can therefore be matched by the directory name itself — which is
 * exactly how `e2e-5` failed CI against `workbench-e2e-54ic0X`. Markers use
 * their own prefix (`term1-`, `term2-`); the random suffix has no hyphen, so
 * it can never manufacture one.
 */
const WORKSPACE_PREFIX = "workbench-e2e-";

/** Seeded file the fake agent's scripted `Read` targets (first file by name). */
export const NOTES_FILE = "notes.md";
export const NOTES_MARKER = "SE3 battery notes";
/** Seeded folder the file-tree journey creates a file in. */
export const SRC_DIR = "src";
/** Seeded office document — opened to assert the host path, never edited. */
export const DOCX_FILE = "sample.docx";
/**
 * Two more, named so the *fake host backend* takes a chosen branch on them
 * (`services/office_host/fake_backend.py` matches the filename): the document
 * somebody else already has open, and the window that refuses to dock. They
 * exist so the office journey can assert that a refusal is an explanation with
 * a way out, without a line of test-only server code.
 *
 * **Every name here must sort after `notes.md`.** The fake agent's scripted
 * `Read` targets the first file in the workspace by name, and journey 4 asserts
 * which file that is — a fixture called `already-…` quietly retargeted it.
 */
export const DOCX_ALREADY_OPEN = "sample-already-open.docx";
export const DOCX_REFUSES_EMBED = "sample-refuse-embed.docx";
/**
 * A workbook, and one whose window refuses to dock. Excel docks through the same
 * host lifecycle as Word (`services/office_host/`), so these prove the office
 * journey's Word assertions hold for Excel too: an ``.xlsx`` docks and the panel
 * names Microsoft Excel, and a refused embed still ends in a working editor. The
 * `-refuse-embed` name is what the fake host backend matches to take that branch.
 */
export const XLSX_FILE = "sample.xlsx";
export const XLSX_REFUSES_EMBED = "sample-refuse-embed.xlsx";
/**
 * The document that docks and then will not quit — the `close_failed` window,
 * sitting on the desktop with an unsaved edit in it.
 *
 * Journey 10 uses it: a workspace switch has to *stop* for this, because the
 * only surface that can say a real Word window was left behind is the panel the
 * switch would have unmounted.
 */
export const DOCX_REFUSES_CLOSE = "sample-refuse-close.docx";
/** A deck that docks, like the .docx and .xlsx beside it. */
export const PPTX_FILE = "slides.pptx";
/** A deck that cannot dock because PowerPoint is already running — the
 * `app-running` trigger, which is the one refusal unique to PowerPoint being
 * single-instance. */
export const PPTX_APP_RUNNING = "slides-app-running.pptx";
/** Body of the working shell shortcut; the terminal journey asserts this text
 * lands on the prompt line and that nothing ever ran it. */
export const SHORTCUT_NAME = "Show the marker";
export const SHORTCUT_BODY = "echo e2e-shortcut-marker";
/** Name of the deliberately malformed entry, echoed in the problems toast. */
export const BROKEN_SHORTCUT_NAME = "Broken entry";
/**
 * The welcome card's dismissal, seeded **dismissed**.
 *
 * The card opens itself as a tab on a window nobody has arranged, which is
 * exactly what a fresh temp workspace is — so without this every journey after
 * the first would run against a five-tab window and `panes.spec.ts` would count
 * one panel too many. Seeding it says what is true of this workspace: it is one
 * the suite has used before.
 *
 * `discover.spec.ts` owns the other side of it — it clears this file through
 * the app's own API to get the first-run window back, and puts it as it found
 * it afterwards, so the journey holds whatever order the suite runs in.
 */
export const WELCOME_FILE = ".workbench/welcome.json";

/**
 * The Setup walkthrough's dismissal, seeded **dismissed** — the same reasoning
 * as `WELCOME_FILE`. Setup auto-opens as a centre tab on a window nobody has
 * arranged (a fresh temp workspace is exactly that), so without this every
 * journey after the first would run against a window with an extra Setup tab and
 * `panes.spec.ts` would count one panel too many. `firstrun.spec.ts` owns the
 * other side: it clears this through the app's own API to get first run back,
 * and puts it as it found it.
 */
export const SETUP_FILE = ".workbench/setup.json";

/** The `layout` entry: the one shortcut kind that acts rather than inserts.
 * Journey 9 presses its chord and asserts the panels moved. */
export const LAYOUT_SHORTCUT_NAME = "Fleet view";
export const LAYOUT_SHORTCUT_TARGET = "Agents";
export const LAYOUT_SHORTCUT_CHORD = "Alt+Y";
/** The `command` kind (M5 item 4): a file binds a *registered* command by id.
 * Journey 3 presses its chord and asserts the safe command ran. */
export const COMMAND_SHORTCUT_NAME = "Flip the theme";
export const COMMAND_SHORTCUT_TARGET = "view.toggleTheme";
export const COMMAND_SHORTCUT_CHORD = "Alt+J";
/** …and one binding a command that re-points the path jail (`workspace.open`,
 * ROADMAP item 5). It parses fine but the UI refuses to run it — the whole point
 * of the untrusted-file bar. */
export const UNSAFE_COMMAND_SHORTCUT_NAME = "Escape the jail";
export const UNSAFE_COMMAND_SHORTCUT_TARGET = "workspace.open";
export const UNSAFE_COMMAND_SHORTCUT_CHORD = "Alt+U";

/**
 * Two folders called `target`, seeded to be told apart only by `CACHEDIR.TAG`.
 *
 * `BUILD_CACHE_DIR` is a cargo build tree, down to the path that put this here:
 * a Tauri build left `popup.toml` twelve levels deep and the QuickBar offered it
 * as a file to open. `OWN_TARGET_FILE` is the other half — an analyst's folder
 * of target data, same name, no tag, and its files must stay reachable.
 */
export const BUILD_CACHE_DIR = "desktop/src-tauri/target";
export const BUILD_ARTIFACT = "debug/build/tauri-7b7005a/out/permissions/menu/commands/popup.toml";
export const OWN_TARGET_FILE = "analysis/target/se3-targets-2026.csv";
/** Cache Directory Tagging Specification — https://bford.info/cachedir/ */
const CACHEDIR_TAG = "Signature: 8a477f597d28d172789f06886806bc55\n";

const SHORTCUTS_FILE = `# E2E shortcuts

## ${SHORTCUT_NAME}
keys: Alt+G

\`\`\`
${SHORTCUT_BODY}
\`\`\`

## ${LAYOUT_SHORTCUT_NAME}
type: layout
keys: ${LAYOUT_SHORTCUT_CHORD}

\`\`\`
${LAYOUT_SHORTCUT_TARGET}
\`\`\`

## ${COMMAND_SHORTCUT_NAME}
type: command
keys: ${COMMAND_SHORTCUT_CHORD}

\`\`\`
${COMMAND_SHORTCUT_TARGET}
\`\`\`

## ${UNSAFE_COMMAND_SHORTCUT_NAME}
type: command
keys: ${UNSAFE_COMMAND_SHORTCUT_CHORD}

\`\`\`
${UNSAFE_COMMAND_SHORTCUT_TARGET}
\`\`\`

## ${BROKEN_SHORTCUT_NAME}
type: shell

\`\`\`
echo one
echo two
\`\`\`
`;

// ---- Claude Code's conversation store, seeded ------------------------------
//
// The suite points `WORKBENCH_CLAUDE_PROJECTS_DIR` away from `~/.claude` so the
// developer's real session history is never read or listed. That leaves the
// directory *empty*, which is the one state the conversation browser has
// nothing to say about — so it is seeded with one transcript per case the
// browser has to tell apart.
//
// **A sibling of the workspace, never inside it** (the perf lane's convention,
// `e2e/perf/workspace.ts`). Inside, the store is a folder in the file tree, and
// it is the alphabetically first one: journey 1 asserts which row `Home` lands
// on, and a transcript directory silently became the answer. What we seed for
// one journey must not be scenery in another's.

/** Where the backend is told to read transcripts from, for a given workspace. */
export const projectsDirFor = (root: string): string => `${root}-projects`;

/**
 * The sibling folder the half-B "switch and open" conversation lives in.
 *
 * A *sibling* of the run's workspace, on the same reasoning as `SECOND_WORKSPACE`
 * and the `-projects` store: outside the workspace (so it is genuinely refused
 * until a switch), small (so switching into it costs a tree of one file), and
 * with a name that shares no substring with the workspace's — the status chip
 * and picker rows are matched by text, so a name that could prefix another's is
 * the bug `SECOND_WORKSPACE` documents. Derived from `root` rather than from the
 * `E2E_WORKSPACE` export because it is needed *while* that export is still being
 * computed (inside `seed`).
 */
const outsideWorkspaceFor = (root: string): string =>
  path.join(
    path.dirname(root),
    `wb-outside-${path.basename(root).slice(WORKSPACE_PREFIX.length)}`,
  );

/**
 * Where the worktree pool puts this run's slots.
 *
 * A *sibling* of the workspace, for both of the reasons the real pool root is
 * outside it (`services/worktrees.py`): a slot inside the workspace would put a
 * whole second checkout in journey 1's file tree and turn every `worktree add`
 * into a watcher storm. Overriding the default matters here too — the shipped
 * one is under `%LOCALAPPDATA%`, keyed by workspace, and a suite that used it
 * would accumulate a real checkout per E2E run on the developer's machine.
 */
export const worktreeRootFor = (root: string): string => `${root}-worktrees`;

/** Claude Code's project-dir encoding, mirrored from
 * `services/session_index.py`. Lossy on purpose: it is what makes the browser's
 * "resolved or honestly encoded" distinction necessary in the first place. */
const encodeProject = (folder: string): string => folder.replace(/[^A-Za-z0-9]/g, "-");

/** Conversation in the workspace root — the one journey 11 searches for. */
export const CONV_ROOT_ID = "11111111-1111-4111-8111-111111111111";
export const CONV_ROOT_TITLE = "Fix the DST bug in the SE3 settlement window";
/** …and a second one there, so a search has something to exclude. */
export const CONV_ROOT_OTHER_ID = "22222222-2222-4222-8222-222222222222";
export const CONV_ROOT_OTHER_TITLE = "Weekly battery availability report";
/** Conversation in `src/` — the one journey 11 opens into a pane. */
export const CONV_SRC_ID = "33333333-3333-4333-8333-333333333333";
export const CONV_SRC_TITLE = "Rewrite the bid curve for the intraday market";
export const CONV_SRC_REPLY = "Here is the bid curve rewrite.";
/** A transcript that will not parse: it keeps its row and says why. */
export const CONV_BROKEN_ID = "44444444-4444-4444-8444-444444444444";
/**
 * A conversation from a folder that resolves but sits *outside* the workspace.
 *
 * Half A showed it and refused it; half B (M5 item 12) opens it by switching the
 * workspace to its folder first, so that folder has to be a real directory the
 * suite can switch into — a dedicated sibling (`OUTSIDE_WORKSPACE`), never the
 * home directory a switch would try to load whole. It carries one marker file so
 * the switch can be proven to have re-rooted the tree.
 */
export const CONV_OUTSIDE_ID = "55555555-5555-4555-8555-555555555555";
export const CONV_OUTSIDE_TITLE = "Something I did in another project";
/** Only in `OUTSIDE_WORKSPACE` — the tree assertion after the switch lands. */
export const OUTSIDE_FILE = "only-in-outside.md";
/** …and one whose folder matches no directory at all: shown under the raw key. */
export const CONV_GONE_KEY = "C--e2e-a-project-that-no-longer-exists";
export const CONV_GONE_ID = "66666666-6666-4666-8666-666666666666";
export const CONV_GONE_TITLE = "A conversation whose folder was deleted";

type Record_ = { type: string; message: { role: string; content: unknown } };

const said = (text: string): Record_ => ({
  type: "user",
  message: { role: "user", content: text },
});
const replied = (text: string): Record_ => ({
  type: "assistant",
  message: { role: "assistant", content: [{ type: "text", text }] },
});
/** Stored as a `user` record by Claude Code, and never a turn. */
const toolResult = (): Record_ => ({
  type: "user",
  message: { role: "user", content: [{ type: "tool_result", content: "ok" }] },
});

function writeTranscript(
  root: string,
  key: string,
  sessionId: string,
  records: Record_[],
): void {
  const directory = path.join(projectsDirFor(root), key);
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(
    path.join(directory, `${sessionId}.jsonl`),
    records.map((record) => JSON.stringify(record)).join("\n") + "\n",
    "utf-8",
  );
}

function seedConversations(root: string): void {
  // The *canonical* path, because the server resolves its workspace root and
  // the encoded key has to match what Python's `Path.resolve()` produces.
  const real = fs.realpathSync.native(root);
  const rootKey = encodeProject(real);
  const srcKey = encodeProject(path.join(real, SRC_DIR));

  writeTranscript(root, rootKey, CONV_ROOT_ID, [
    said(CONV_ROOT_TITLE),
    replied("Looking at the settlement window."),
    toolResult(), // present so the turn count proves it is not counted
    said("and check the autumn transition"),
  ]);
  writeTranscript(root, rootKey, CONV_ROOT_OTHER_ID, [said(CONV_ROOT_OTHER_TITLE)]);
  writeTranscript(root, srcKey, CONV_SRC_ID, [
    said(CONV_SRC_TITLE),
    replied(CONV_SRC_REPLY),
  ]);
  // A real sibling directory, seeded fresh: the conversation is outside the
  // workspace (so half B has something to switch to), and the marker file proves
  // the tree followed once it does.
  const outside = outsideWorkspaceFor(root);
  fs.rmSync(outside, { recursive: true, force: true });
  fs.mkdirSync(outside, { recursive: true });
  fs.writeFileSync(
    path.join(outside, OUTSIDE_FILE),
    "# Another project\n\nOnly reachable after switching the workspace here.\n",
    "utf-8",
  );
  writeTranscript(root, encodeProject(fs.realpathSync.native(outside)), CONV_OUTSIDE_ID, [
    said(CONV_OUTSIDE_TITLE),
    replied("Picking up where we left off."),
  ]);
  writeTranscript(root, CONV_GONE_KEY, CONV_GONE_ID, [said(CONV_GONE_TITLE)]);

  // Not JSON at all, and truncated mid-record: the browser must keep the row.
  fs.writeFileSync(
    path.join(projectsDirFor(root), rootKey, `${CONV_BROKEN_ID}.jsonl`),
    '}}} not json\n{"type":"user","message":{"role":"user","content":"cut off her',
    "utf-8",
  );
}

function seed(root: string): void {
  fs.mkdirSync(path.join(root, SRC_DIR));
  fs.writeFileSync(path.join(root, SRC_DIR, "model.py"), "PRICE_AREA = 'SE3'\n", "utf-8");
  fs.writeFileSync(path.join(root, NOTES_FILE), `# Notes\n\n${NOTES_MARKER}.\n`, "utf-8");
  // Never opened by an editor. The office journey runs against the *fake* host
  // backend with no Document Server configured, so nothing ever reads a byte of
  // these — their *names* choose a branch of the host lifecycle, one per
  // application and one per refusal worth a journey.
  for (const name of [
    DOCX_FILE,
    DOCX_ALREADY_OPEN,
    DOCX_REFUSES_EMBED,
    DOCX_REFUSES_CLOSE,
    XLSX_FILE,
    XLSX_REFUSES_EMBED,
    PPTX_FILE,
    PPTX_APP_RUNNING,
  ]) {
    fs.writeFileSync(path.join(root, name), "not a real document\n", "utf-8");
  }
  fs.mkdirSync(path.join(root, ".workbench"));
  fs.writeFileSync(path.join(root, ".workbench", "shortcuts.md"), SHORTCUTS_FILE, "utf-8");
  fs.writeFileSync(
    path.join(root, ...WELCOME_FILE.split("/")),
    `${JSON.stringify({ dismissed: true }, null, 2)}\n`,
    "utf-8",
  );
  fs.writeFileSync(
    path.join(root, ...SETUP_FILE.split("/")),
    `${JSON.stringify({ dismissed: true }, null, 2)}\n`,
    "utf-8",
  );
  seedTargetFolders(root);
  seedConversations(root);
  seedRepository(root);
}

/**
 * Make the workspace a real git repository, so the worktree pool can serve.
 *
 * Mission Control's workers each run in a **borrowed worktree** — that is the
 * whole reason two of them cannot step on each other (`services/worktrees.py`)
 * — and the pool refuses to hand out a slot when the workspace is not a
 * repository. Without this, journey 12 would test the refusal rather than the
 * feature, and a refusal is already covered by `test_orchestrator.py`.
 *
 * `.git` is in the file tree's `IGNORED_DIRS`, so nothing else in the suite
 * sees it. If git is missing (it never is on a machine that checked this repo
 * out) the seed is skipped and the journey's own preflight says why.
 */
function seedRepository(root: string): void {
  const git = (...args: string[]): void => {
    child_process.execFileSync("git", args, { cwd: root, stdio: "ignore" });
  };
  try {
    git("init", "-b", "main");
    git("config", "user.email", "e2e@workbench.invalid");
    git("config", "user.name", "Workbench E2E");
    // `core.autocrlf` off for the same reason `test_worktrees.py` sets it: a
    // developer machine with it on globally would check the slot out with CRLF
    // and change bytes the journeys read.
    git("config", "core.autocrlf", "false");
    git("add", "-A");
    git("-c", "commit.gpgsign=false", "commit", "-m", "e2e workspace");
  } catch {
    // Left to the journey to report — a seed that threw here would take the
    // other eleven journeys down with it.
  }
}

/** The build cache that must vanish, and the folder of the same name that must not. */
function seedTargetFolders(root: string): void {
  const cache = path.join(root, ...BUILD_CACHE_DIR.split("/"));
  const artifact = path.join(cache, ...BUILD_ARTIFACT.split("/"));
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  fs.writeFileSync(path.join(cache, "CACHEDIR.TAG"), CACHEDIR_TAG, "utf-8");
  fs.writeFileSync(artifact, '[[permission]]\nidentifier = "allow-popup"\n', "utf-8");

  const own = path.join(root, ...OWN_TARGET_FILE.split("/"));
  fs.mkdirSync(path.dirname(own), { recursive: true });
  fs.writeFileSync(own, "hour,mw\n2026-01-01T00:00,4.2\n", "utf-8");
}

/** Validate a requested workspace: it must be an empty or nonexistent directory. */
function prepare(requested: string): string {
  const root = path.resolve(requested);
  if (!fs.existsSync(root)) {
    fs.mkdirSync(root, { recursive: true });
    return root;
  }
  if (!fs.statSync(root).isDirectory()) {
    throw new Error(`${WORKSPACE_ENV}=${root} is not a directory.`);
  }
  if (fs.readdirSync(root).length > 0) {
    throw new Error(
      `${WORKSPACE_ENV}=${root} is not empty. The suite seeds the workspace it runs in and ` +
        `the journeys write to it, so it must start empty — unset the variable for a fresh ` +
        `temp directory, or point it at an empty (or nonexistent) path.`,
    );
  }
  return root;
}

function ensureWorkspace(): string {
  const active = process.env[ACTIVE_ENV];
  if (active !== undefined && active !== "") return active; // a worker: already seeded
  const requested = process.env[WORKSPACE_ENV];
  const root =
    requested !== undefined && requested !== ""
      ? prepare(requested)
      : fs.mkdtempSync(path.join(os.tmpdir(), WORKSPACE_PREFIX));
  seed(root);
  process.env[ACTIVE_ENV] = root;
  process.env[WORKSPACE_ENV] = root; // resolved, for anything reading it downstream
  return root;
}

/** True in the runner, false in every worker it hands the seeded path to.
 * Read *before* `ensureWorkspace()` publishes the path, because publishing it
 * is what makes the two indistinguishable afterwards. */
const IS_RUNNER = (process.env[ACTIVE_ENV] ?? "") === "";

export const E2E_WORKSPACE = ensureWorkspace();

/** Claude Code's project key for the seeded `src/` conversations — which is
 * also the instance key of a browser pane scoped to that folder, and therefore
 * the exact string journey 11 expects to find in `.workbench/layouts.json`. */
export const CONV_SRC_PROJECT_KEY = encodeProject(
  path.join(fs.realpathSync.native(E2E_WORKSPACE), SRC_DIR),
);

/* ---- the second workspace, for the switcher journey ------------------------
 *
 * A *sibling* of the run's workspace, never a folder inside it: the switcher's
 * whole claim is that the tree, the layout file and shortcuts follow the root,
 * and a second project nested in the first would be visible from both — so the
 * assertion "this file is not here any more" would prove nothing. Seeded with
 * one file, one shortcut and one saved layout, each named so it cannot be
 * confused with the first workspace's.
 */

/**
 * Where the second workspace lives, and where machine-local state goes. Both
 * siblings, on the same reasoning as the perf lane's `-projects` sibling.
 *
 * The second workspace's **folder name shares no substring with the first's**,
 * and that is load-bearing rather than tidy. `${E2E_WORKSPACE}-second` reads
 * better and cost an hour: the picker rows and the status chip are matched by
 * text, so `workbench-e2e-21NheM` matched the row for
 * `workbench-e2e-21NheM-second` first — the row for the workspace you are
 * already in, which is deliberately not choosable. The suite already learned
 * this once (`WORKSPACE_PREFIX` above, and `e2e-5` matching the directory it
 * was running in); the answer both times is a name that cannot be a prefix.
 */
export const SECOND_WORKSPACE = path.join(
  path.dirname(E2E_WORKSPACE),
  `wb-other-${path.basename(E2E_WORKSPACE).slice(WORKSPACE_PREFIX.length)}`,
);
export const E2E_APP_DATA = `${E2E_WORKSPACE}-appdata`;

/**
 * The sibling that journey 11's "switch and open" conversation lives in, seeded
 * by `seedConversations` above. Exported so the spec can name the folder it
 * expects the window to re-root into and, afterwards, put the workspace back.
 */
export const OUTSIDE_WORKSPACE = outsideWorkspaceFor(E2E_WORKSPACE);

/**
 * Remove the outside workspace once journey 11 is done with it.
 *
 * Switching into it records it on the machine-local recents list, which lives in
 * the backend for the rest of the run — and a *non-current* recent becomes a
 * dynamic "Open workspace …" command that later journeys (the QuickBar's exact
 * category list) do not expect. The recents' `exists` flag is re-stated on every
 * read and the command builder drops a folder that is gone, so deleting the
 * throwaway sibling is what keeps this journey from leaking a command into the
 * ones that run after it. Idempotent, with retries for a watch handle the switch
 * back may not have released yet on Windows.
 */
export function removeOutsideWorkspace(): void {
  fs.rmSync(OUTSIDE_WORKSPACE, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}

/** Only in the second workspace — the file tree assertion, both ways. */
export const SECOND_FILE = "only-in-second.md";
/** Only in the second workspace's `.workbench/shortcuts.md`. */
export const SECOND_SHORTCUT_NAME = "Second workspace only";
/** Only in the second workspace's `.workbench/layouts.json`. */
export const SECOND_LAYOUT_NAME = "Second view";

const SECOND_SHORTCUTS_FILE = `# Second workspace shortcuts

## ${SECOND_SHORTCUT_NAME}

\`\`\`
echo second-workspace-marker
\`\`\`
`;

/**
 * A layouts document whose *saved* list is the only thing under test.
 *
 * `current` is null on purpose, so nothing is applied to the dock on arrival:
 * the claim is that this workspace's saved names are the ones on the menu, and
 * applying a hand-written dockview grid would be testing dockview. The server
 * stores the `state` verbatim (its shape belongs to a UI library), so `{}` is a
 * legal document that the menu lists and nothing tries to restore.
 */
const SECOND_LAYOUTS_FILE = JSON.stringify({
  current: null,
  current_name: null,
  saved: [{ name: SECOND_LAYOUT_NAME, state: {} }],
});

function seedSecondWorkspace(): void {
  // Left behind between runs like the main workspace, so re-seeding must be
  // idempotent rather than assume an empty directory.
  fs.rmSync(SECOND_WORKSPACE, { recursive: true, force: true });
  fs.mkdirSync(path.join(SECOND_WORKSPACE, ".workbench"), { recursive: true });
  fs.writeFileSync(
    path.join(SECOND_WORKSPACE, SECOND_FILE),
    "# The other project\n\nNothing from the first workspace is here.\n",
    "utf-8",
  );
  fs.writeFileSync(
    path.join(SECOND_WORKSPACE, ".workbench", "shortcuts.md"),
    SECOND_SHORTCUTS_FILE,
    "utf-8",
  );
  fs.writeFileSync(
    path.join(SECOND_WORKSPACE, ".workbench", "layouts.json"),
    SECOND_LAYOUTS_FILE,
    "utf-8",
  );
  // The recent-workspaces list is machine-local state, not a project's — so a
  // run must not read or rewrite the developer's real one. Started empty each
  // run, which is also what makes the recents assertions deterministic.
  fs.rmSync(E2E_APP_DATA, { recursive: true, force: true });
  fs.mkdirSync(E2E_APP_DATA, { recursive: true });
}

// Runner only. A worker re-seeding would wipe the recent list *after* the
// server had already recorded its launch workspace in it, which is exactly the
// row the journey asserts you can come back to.
if (IS_RUNNER) seedSecondWorkspace();

/** Absolute path of a workspace-relative file, for on-disk edits from a test. */
export function workspacePath(relative: string): string {
  return path.join(E2E_WORKSPACE, ...relative.split("/"));
}

export function readWorkspaceFile(relative: string): string {
  return fs.readFileSync(workspacePath(relative), "utf-8");
}

/** Write a file from outside the app — the "someone else changed it" half of
 * every watcher assertion. Missing parents are created, so one call can stand
 * for a build starting: a new directory and a file inside it. */
export function writeWorkspaceFile(relative: string, content: string): void {
  const target = workspacePath(relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf-8");
}

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
 * journey left, next to the Playwright trace.
 */

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
/** A deck, for the one application v1 deliberately does not dock. */
export const PPTX_FILE = "slides.pptx";
/** Body of the working shell shortcut; the terminal journey asserts this text
 * lands on the prompt line and that nothing ever ran it. */
export const SHORTCUT_NAME = "Show the marker";
export const SHORTCUT_BODY = "echo e2e-shortcut-marker";
/** Name of the deliberately malformed entry, echoed in the problems toast. */
export const BROKEN_SHORTCUT_NAME = "Broken entry";
/** The `layout` entry: the one shortcut kind that acts rather than inserts.
 * Journey 9 presses its chord and asserts the panels moved. */
export const LAYOUT_SHORTCUT_NAME = "Fleet view";
export const LAYOUT_SHORTCUT_TARGET = "Agents";
export const LAYOUT_SHORTCUT_CHORD = "Alt+Y";

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

## ${BROKEN_SHORTCUT_NAME}
type: shell

\`\`\`
echo one
echo two
\`\`\`
`;

// ---- Claude Code's conversation store, seeded ------------------------------
//
// `playwright.config.ts` points `WORKBENCH_CLAUDE_PROJECTS_DIR` at
// `<workspace>/.claude-projects`, so the developer's real session history is
// never read. That leaves the directory *empty*, which is exactly the state
// the conversation browser has nothing to say about — so the journey seeds it
// with one of each case the browser has to tell apart.

/** Directory the backend is told to read transcripts from (config.ts agrees). */
const PROJECTS_DIR = ".claude-projects";

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
/** A conversation from a folder that resolves but sits outside the workspace —
 * the half-B refusal, shown rather than hidden. The home directory is used
 * because it certainly exists; nothing is ever written to it. */
export const CONV_OUTSIDE_ID = "55555555-5555-4555-8555-555555555555";
export const CONV_OUTSIDE_TITLE = "Something I did in another project";
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
  const directory = path.join(root, PROJECTS_DIR, key);
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
  writeTranscript(root, encodeProject(fs.realpathSync.native(os.homedir())), CONV_OUTSIDE_ID, [
    said(CONV_OUTSIDE_TITLE),
  ]);
  writeTranscript(root, CONV_GONE_KEY, CONV_GONE_ID, [said(CONV_GONE_TITLE)]);

  // Not JSON at all, and truncated mid-record: the browser must keep the row.
  fs.writeFileSync(
    path.join(root, PROJECTS_DIR, rootKey, `${CONV_BROKEN_ID}.jsonl`),
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
  // these — three of them are named to choose a branch of the host lifecycle,
  // and the fourth is the application v1 will not dock.
  for (const name of [DOCX_FILE, DOCX_ALREADY_OPEN, DOCX_REFUSES_EMBED, PPTX_FILE]) {
    fs.writeFileSync(path.join(root, name), "not a real document\n", "utf-8");
  }
  fs.mkdirSync(path.join(root, ".workbench"));
  fs.writeFileSync(path.join(root, ".workbench", "shortcuts.md"), SHORTCUTS_FILE, "utf-8");
  seedTargetFolders(root);
  seedConversations(root);
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

export const E2E_WORKSPACE = ensureWorkspace();

/** Claude Code's project key for the seeded `src/` conversations — which is
 * also the instance key of a browser pane scoped to that folder, and therefore
 * the exact string journey 11 expects to find in `.workbench/layouts.json`. */
export const CONV_SRC_PROJECT_KEY = encodeProject(
  path.join(fs.realpathSync.native(E2E_WORKSPACE), SRC_DIR),
);

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

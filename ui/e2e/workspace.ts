/**
 * The per-run temp workspace the E2E backend is launched in.
 *
 * Created once per `playwright test` run and seeded with everything the
 * journeys need: a folder to create files in, a text file the fake agent can
 * Read, an office document (for degraded mode), a `.workbench/shortcuts.md`
 * carrying one working shortcut and one malformed entry, and the two folders
 * named `target` that journey 3 uses to prove build caches are skipped by their
 * `CACHEDIR.TAG` rather than by their name.
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
/** Seeded office document — opened to assert degraded mode, never edited. */
export const DOCX_FILE = "sample.docx";
/** Body of the working shell shortcut; the terminal journey asserts this text
 * lands on the prompt line and that nothing ever ran it. */
export const SHORTCUT_NAME = "Show the marker";
export const SHORTCUT_BODY = "echo e2e-shortcut-marker";
/** Name of the deliberately malformed entry, echoed in the problems toast. */
export const BROKEN_SHORTCUT_NAME = "Broken entry";

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

## ${BROKEN_SHORTCUT_NAME}
type: shell

\`\`\`
echo one
echo two
\`\`\`
`;

function seed(root: string): void {
  fs.mkdirSync(path.join(root, SRC_DIR));
  fs.writeFileSync(path.join(root, SRC_DIR, "model.py"), "PRICE_AREA = 'SE3'\n", "utf-8");
  fs.writeFileSync(path.join(root, NOTES_FILE), `# Notes\n\n${NOTES_MARKER}.\n`, "utf-8");
  // Never opened by an editor — the office journey runs with no Document Server
  // configured, so the panel shows the degraded card without reading a byte.
  fs.writeFileSync(path.join(root, DOCX_FILE), "not a real document\n", "utf-8");
  fs.mkdirSync(path.join(root, ".workbench"));
  fs.writeFileSync(path.join(root, ".workbench", "shortcuts.md"), SHORTCUTS_FILE, "utf-8");
  seedTargetFolders(root);
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

/** Absolute path of a workspace-relative file, for on-disk edits from a test. */
export function workspacePath(relative: string): string {
  return path.join(E2E_WORKSPACE, ...relative.split("/"));
}

export function readWorkspaceFile(relative: string): string {
  return fs.readFileSync(workspacePath(relative), "utf-8");
}

/** Write a file from outside the app — the "someone else changed it" half of
 * every watcher assertion. */
export function writeWorkspaceFile(relative: string, content: string): void {
  fs.writeFileSync(workspacePath(relative), content, "utf-8");
}

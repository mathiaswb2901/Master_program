/**
 * The per-run temp workspace the E2E backend is launched in.
 *
 * Created once per `playwright test` run and seeded with everything the
 * journeys need: a folder to create files in, a text file the fake agent can
 * Read, an office document (for degraded mode) and a `.workbench/shortcuts.md`
 * carrying one working shortcut and one malformed entry.
 *
 * Why the env var: `playwright.config.ts` is loaded by the runner *and* by every
 * worker process, so creating the directory unconditionally would give each
 * worker a different workspace. The runner creates it first and publishes the
 * path through `process.env`, which workers inherit — so `E2E_WORKSPACE` is the
 * same directory everywhere, in the config, in the specs, and in the server.
 *
 * The directory is deliberately left behind: it holds the exact state a failing
 * journey left, next to the Playwright trace.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const WORKSPACE_ENV = "WB_E2E_WORKSPACE";

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
}

function ensureWorkspace(): string {
  const existing = process.env[WORKSPACE_ENV];
  if (existing !== undefined && existing !== "") return existing;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "workbench-e2e-"));
  seed(root);
  process.env[WORKSPACE_ENV] = root;
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

/**
 * The perf lane's workspace: 5,005 files, generated at test time.
 *
 * The shape is defined once, in Python, at `server/tests/perf_fixture.py` —
 * the pytest budgets import it and this shells out to it, so both halves of the
 * lane measure the same workspace. A second definition in TypeScript would
 * drift, and two lanes measuring two different workspaces is worse than one
 * lane. The one thing added on this side is the app's own `.workbench/` state,
 * which is not workspace content and not part of the counted shape — see
 * `seedWindowState`.
 *
 * Fresh per run by default. A perf journey writes into the workspace (the
 * watcher budget creates files), so reusing a directory a previous run left
 * behind would change the file counts the budgets are stated against.
 * `WB_PERF_WORKSPACE` is the local-iteration knob: point it somewhere stable and
 * the generator's own stamp check skips the ~3 s rebuild.
 *
 * Fresh per run also means *gone* after the run: a fixture this module created
 * is removed on the way out, along with the projects sibling the backend makes
 * next to it. Who owns what, and the sweep for runs that never got to exit, are
 * in `workspace.ts` — see `discardOnExit` below for why the removal is not a
 * Playwright `globalTeardown`.
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  ACTIVE_ENV,
  OWNED_ENV,
  STAMP_ENV,
  TMP_PREFIX,
  WORKSPACE_ENV,
  discardWorkspace,
  pruneStaleWorkspaces,
} from "./workspace";

export { WORKSPACE_ENV };

/** Repo root: this file lives in `<root>/ui/e2e/perf`. */
export const REPO_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..", "..");
const GENERATOR = path.join(REPO_ROOT, "server", "tests", "perf_fixture.py");

/** What the generator prints on stdout — the numbers the budgets quote. */
export interface FixtureStamp {
  spec_version: number;
  root: string;
  visible_files: number;
  visible_dirs: number;
  top_level_dirs: string[];
}

function generate(root: string): FixtureStamp {
  fs.mkdirSync(root, { recursive: true });
  // `uv run --project` so the generator runs in the repo's environment without
  // this process caring where the venv is — the same call playwright.config.ts
  // starts the backend with.
  const stdout = execFileSync(
    "uv",
    ["run", "--project", REPO_ROOT, "python", GENERATOR, root],
    { encoding: "utf-8", stdio: ["ignore", "pipe", "inherit"] },
  );
  const line = stdout.trim().split("\n").pop() ?? "";
  return JSON.parse(line) as FixtureStamp;
}

/**
 * The window state a *used* workspace already has, seeded before the first
 * launch is timed.
 *
 * This lane measures the app, not its scaffolding. A workspace with no
 * `.workbench/welcome.json` is first run by definition, so the discovery panel
 * opens itself over every `page.goto("/")` here — an extra `GET`, an extra
 * panel and 48 more rows on a page whose whole purpose is a launch budget
 * (`launch.spec.ts`), and, once the layout autosave has run, a Keyboard pane
 * baked into the saved arrangement every later spec restores. Seeding it says
 * what is true of the workspace being measured: somebody has been here before.
 * `ui/e2e/workspace.ts` does exactly this for the journey suite and for exactly
 * the same reason.
 *
 * Why here and not in `perf_fixture.py`, which owns the shape: `.workbench/` is
 * the *app's* state directory, not workspace content — the running app creates
 * it itself a second later. The generator's counted shape is the walk the
 * pytest budgets assert against (5,005 files, 16 directories), and those
 * budgets never launch a browser, so a browser's window state has no business
 * moving their numbers.
 */
function seedWindowState(root: string): void {
  const dir = path.join(root, ".workbench");
  fs.mkdirSync(dir, { recursive: true });
  const welcome = `${JSON.stringify({ dismissed: true }, null, 2)}\n`;
  fs.writeFileSync(path.join(dir, "welcome.json"), welcome, "utf-8");
}

/**
 * Remove the fixture when this process ends.
 *
 * Not Playwright's `globalTeardown`, and the reason is ordering. Playwright
 * tears its setup tasks down in reverse, and the `webServer` processes are set
 * up *first* — so a `globalTeardown` hook runs while the backend is still alive
 * with the fixture as its working directory and a watcher handle on every
 * directory inside it. Windows lets you delete neither. `exit` fires after
 * Playwright has stopped the servers, which is the first moment the directory
 * is actually free.
 *
 * Only the runner gets here: a worker returns from `ensureFixture` above, at the
 * branch that finds the environment already stamped. That matters — a worker
 * exiting must never remove a fixture the run is still measuring.
 */
function discardOnExit(root: string): void {
  process.once("exit", () => {
    try {
      discardWorkspace(root);
    } catch {
      // A handle still held on the way out is not worth an error printed under
      // the report. `pruneStaleWorkspaces` sweeps it on a later run.
    }
  });
}

function ensureFixture(): FixtureStamp {
  const active = process.env[ACTIVE_ENV];
  const stamped = process.env[STAMP_ENV];
  if (active !== undefined && active !== "" && stamped !== undefined && stamped !== "") {
    return JSON.parse(stamped) as FixtureStamp; // a worker: the runner built it
  }
  const requested = process.env[WORKSPACE_ENV] ?? "";
  const owned = requested === "";
  // Sweep before creating, so a run that was killed before its exit hook does
  // not leave 5,105 files in tmp for good.
  if (owned) pruneStaleWorkspaces(os.tmpdir());
  const root = owned ? fs.mkdtempSync(path.join(os.tmpdir(), TMP_PREFIX)) : path.resolve(requested);
  const stamp = generate(root);
  // After `generate`, and unconditionally: a pinned workspace skips the rebuild
  // but its `.workbench/` is the one thing a previous run *did* change.
  seedWindowState(stamp.root);
  process.env[ACTIVE_ENV] = stamp.root;
  process.env[STAMP_ENV] = JSON.stringify(stamp);
  process.env[OWNED_ENV] = owned ? "1" : "";
  if (owned) discardOnExit(stamp.root);
  return stamp;
}

export const FIXTURE = ensureFixture();
export const PERF_WORKSPACE = FIXTURE.root;

/** Absolute path of a fixture-relative file, for on-disk edits from a test. */
export function fixturePath(relative: string): string {
  return path.join(PERF_WORKSPACE, ...relative.split("/"));
}

export function writeFixtureFile(relative: string, content: string): void {
  fs.writeFileSync(fixturePath(relative), content, "utf-8");
}

export function removeFixtureFile(relative: string): void {
  fs.rmSync(fixturePath(relative), { force: true });
}

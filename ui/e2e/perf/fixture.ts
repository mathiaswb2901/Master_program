/**
 * The perf lane's workspace: 5,005 files, generated at test time.
 *
 * The shape is defined once, in Python, at `server/tests/perf_fixture.py` —
 * the pytest budgets import it and this shells out to it, so both halves of the
 * lane measure the same workspace. A second definition in TypeScript would
 * drift, and two lanes measuring two different workspaces is worse than one
 * lane.
 *
 * Fresh per run by default. A perf journey writes into the workspace (the
 * watcher budget creates files), so reusing a directory a previous run left
 * behind would change the file counts the budgets are stated against.
 * `WB_PERF_WORKSPACE` is the local-iteration knob: point it somewhere stable and
 * the generator's own stamp check skips the ~3 s rebuild.
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** Public: "build this run's fixture here" (and reuse it if it is current). */
export const WORKSPACE_ENV = "WB_PERF_WORKSPACE";
/** Internal: the built path, runner -> workers. Set below, never by hand. */
const ACTIVE_ENV = "WB_PERF_WORKSPACE_ACTIVE";
/** Internal: the generator's stamp, so workers need not re-run it. */
const STAMP_ENV = "WB_PERF_FIXTURE_STAMP";

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

function ensureFixture(): FixtureStamp {
  const active = process.env[ACTIVE_ENV];
  const stamped = process.env[STAMP_ENV];
  if (active !== undefined && active !== "" && stamped !== undefined && stamped !== "") {
    return JSON.parse(stamped) as FixtureStamp; // a worker: the runner built it
  }
  const requested = process.env[WORKSPACE_ENV];
  const root =
    requested !== undefined && requested !== ""
      ? path.resolve(requested)
      : fs.mkdtempSync(path.join(os.tmpdir(), "workbench-perf-"));
  const stamp = generate(root);
  process.env[ACTIVE_ENV] = stamp.root;
  process.env[STAMP_ENV] = JSON.stringify(stamp);
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

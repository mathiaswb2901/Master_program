/**
 * The perf fixture's *directory*: who owns it, and who removes it.
 *
 * Separate from `fixture.ts` because that module builds the fixture the moment
 * it is imported — 5,105 files, ~3 s. Everything here is a function over a path
 * or an environment, so the unit test, the cleanup hook and the config can all
 * import it without generating a workspace.
 *
 * The rule is one line: **a run removes only the directory it created.**
 * `WB_PERF_WORKSPACE` names a directory the developer chose and expects to find
 * again — skipping the rebuild is the entire point of the knob — so a run handed
 * one deletes nothing. A `mkdtemp` directory is ours, and unlike the journey
 * suite's workspace (`ui/e2e/workspace.ts`, deliberately left behind because it
 * holds the state a failing journey left) this one holds nothing worth keeping:
 * it is generated from `server/tests/perf_fixture.py`, byte-identical every run,
 * and what a failed perf run leaves worth reading — the trace, the JSON report —
 * is in `perf-results/`, not in here.
 */

import fs from "node:fs";
import path from "node:path";

/** Public: "build this run's fixture here" (and reuse it if it is current). */
export const WORKSPACE_ENV = "WB_PERF_WORKSPACE";
/** Internal: the built path, runner -> workers. Set by `fixture.ts`, never by hand. */
export const ACTIVE_ENV = "WB_PERF_WORKSPACE_ACTIVE";
/** Internal: the generator's stamp, so workers need not re-run it. */
export const STAMP_ENV = "WB_PERF_FIXTURE_STAMP";
/** Internal: "this run created the active directory", so this run may remove it. */
export const OWNED_ENV = "WB_PERF_FIXTURE_OWNED";

/**
 * Prefix of the temp directories this lane creates — and the safety catch:
 * `discardWorkspace` refuses any path not named this way, so no later caller can
 * point the cleanup at a directory a developer named.
 */
export const TMP_PREFIX = "workbench-perf-";

/**
 * Session storage lives *beside* the fixture, never inside it: inside, it would
 * be a directory the file tree walks and the folder list reports, so the server
 * would be measuring a workspace it had just changed. Being a sibling makes it
 * the second directory a run creates, which is why the config and the cleanup
 * both derive it here rather than each spelling out the suffix.
 */
export function projectsDirFor(root: string): string {
  return `${root}-projects`;
}

/**
 * The directory this run must remove when it exits, or `null` for "nothing is
 * ours". Reading the decision out of an environment rather than recomputing it
 * keeps one answer for the runner, the workers and the exit hook.
 */
export function ownedWorkspace(env: Record<string, string | undefined>): string | null {
  const root = env[ACTIVE_ENV];
  if (env[OWNED_ENV] !== "1" || root === undefined || root === "") return null;
  return root;
}

/** Remove a fixture directory and its projects sibling. Returns what went. */
export function discardWorkspace(root: string): string[] {
  if (!path.basename(root).startsWith(TMP_PREFIX)) {
    throw new Error(`refusing to remove ${root}: not a ${TMP_PREFIX}* directory this lane created`);
  }
  const removed: string[] = [];
  for (const target of [root, projectsDirFor(root)]) {
    if (!fs.existsSync(target)) continue;
    // Windows releases handles lazily, and the backend that just died held a
    // watcher handle on every directory in here — so the first unlink can still
    // see EBUSY. `rmSync` backs off and retries on exactly those errors.
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    removed.push(target);
  }
  return removed;
}

/**
 * How old a leftover must be before a new run sweeps it. A perf run is bounded
 * by the config's 240 s test timeout, so a day is far outside any live run —
 * and "outside" is the only property that matters here. Waiting costs disk;
 * being wrong costs a running fixture.
 */
export const STALE_AFTER_MS = 24 * 60 * 60 * 1_000;

export interface TempDir {
  name: string;
  mtimeMs: number;
}

/** Pure: which of `dirs` are this lane's leftovers, old enough to be dead. */
export function staleWorkspaces(
  dirs: TempDir[],
  now: number,
  staleAfterMs: number = STALE_AFTER_MS,
): string[] {
  return dirs
    .filter((dir) => dir.name.startsWith(TMP_PREFIX) && now - dir.mtimeMs > staleAfterMs)
    .map((dir) => dir.name);
}

/**
 * Sweep leftovers from runs that never reached their exit hook — a hard kill, a
 * machine losing power. The hook covers every ordinary ending, Ctrl-C included;
 * this covers the rest, so "the perf lane leaves nothing behind" survives the
 * crash case too. Never fatal: a leftover is not worth failing a perf run over.
 */
export function pruneStaleWorkspaces(tmpDir: string, now: number = Date.now()): string[] {
  let names: string[];
  try {
    names = fs.readdirSync(tmpDir);
  } catch {
    // No temp directory to sweep is not a problem worth reporting.
    return [];
  }
  const dirs: TempDir[] = [];
  for (const name of names) {
    if (!name.startsWith(TMP_PREFIX)) continue; // stat only our own, not all of tmp
    try {
      const stat = fs.statSync(path.join(tmpDir, name));
      if (stat.isDirectory()) dirs.push({ name, mtimeMs: stat.mtimeMs });
    } catch {
      // Vanished between readdir and stat, or not ours to read. Skip it.
    }
  }
  const removed: string[] = [];
  for (const name of staleWorkspaces(dirs, now)) {
    try {
      removed.push(...discardWorkspace(path.join(tmpDir, name)));
    } catch {
      // Still locked, or already gone. The next run tries again.
    }
  }
  return removed;
}

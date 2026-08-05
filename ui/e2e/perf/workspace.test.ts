/**
 * The perf lane leaves nothing behind — and removes nothing it did not create.
 *
 * The regression this file exists for: `npm run perf` with no
 * `WB_PERF_WORKSPACE` set generated 5,105 files into a fresh `os.tmpdir()`
 * directory, the backend created a `-projects` sibling next to it, and nothing
 * in the lane ever removed either one. Every bare perf run cost another 5,105
 * files of temp, permanently. The other half of the rule is the dangerous half:
 * a developer who *does* set `WB_PERF_WORKSPACE` is naming a directory of their
 * own, and a cleanup that removed it would be a far worse bug than the leak.
 *
 * Imports `workspace.ts` and never `fixture.ts`: importing the latter *builds* a
 * fixture, which is exactly the three seconds and 5,105 files a unit test must
 * not spend.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ACTIVE_ENV,
  OWNED_ENV,
  STALE_AFTER_MS,
  TMP_PREFIX,
  discardWorkspace,
  ownedWorkspace,
  projectsDirFor,
  pruneStaleWorkspaces,
  staleWorkspaces,
} from "./workspace";

/** A stand-in for `os.tmpdir()`, so a test never sweeps the real one. */
let tmp: string;

/** Build `<tmp>/<name>` with one file in it, and return the path. */
function makeDir(name: string): string {
  const dir = path.join(tmp, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "marker.txt"), "x", "utf-8");
  return dir;
}

function ageBy(dir: string, ms: number): void {
  const when = new Date(Date.now() - ms);
  fs.utimesSync(dir, when, when);
}

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "wb-perf-cleanup-"));
});

afterEach(() => {
  fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
});

describe("ownedWorkspace", () => {
  it("claims the directory the run created", () => {
    const env = { [ACTIVE_ENV]: "C:\\tmp\\workbench-perf-ab12", [OWNED_ENV]: "1" };
    expect(ownedWorkspace(env)).toBe("C:\\tmp\\workbench-perf-ab12");
  });

  it("claims nothing when the workspace was supplied", () => {
    // WB_PERF_WORKSPACE was set: the directory is the developer's, kept between
    // runs on purpose. This is the assertion that stops the fix from becoming a
    // worse bug than the leak it closes.
    expect(ownedWorkspace({ [ACTIVE_ENV]: "D:\\work\\perf-fixture", [OWNED_ENV]: "" })).toBeNull();
    expect(ownedWorkspace({ [ACTIVE_ENV]: "D:\\work\\perf-fixture" })).toBeNull();
  });

  it("claims nothing when no fixture was ever built", () => {
    expect(ownedWorkspace({})).toBeNull();
    expect(ownedWorkspace({ [ACTIVE_ENV]: "", [OWNED_ENV]: "1" })).toBeNull();
  });
});

describe("discardWorkspace", () => {
  it("takes the fixture and the projects sibling the backend made", () => {
    const root = makeDir(`${TMP_PREFIX}ab12`);
    const projects = makeDir(`${TMP_PREFIX}ab12-projects`);
    expect(projectsDirFor(root)).toBe(projects);

    expect(discardWorkspace(root)).toEqual([root, projects]);
    expect(fs.existsSync(root)).toBe(false);
    expect(fs.existsSync(projects)).toBe(false);
  });

  it("is fine with a run whose backend never wrote a projects directory", () => {
    const root = makeDir(`${TMP_PREFIX}cd34`);
    expect(discardWorkspace(root)).toEqual([root]);
    expect(fs.existsSync(root)).toBe(false);
  });

  it("refuses a directory this lane did not create", () => {
    const mine = makeDir("se3-price-data");
    expect(() => discardWorkspace(mine)).toThrow(/refusing to remove/);
    expect(fs.existsSync(mine)).toBe(true);
  });
});

describe("staleWorkspaces", () => {
  const now = 1_700_000_000_000;

  it("selects only this lane's directories, and only old ones", () => {
    const dirs = [
      { name: `${TMP_PREFIX}old`, mtimeMs: now - STALE_AFTER_MS - 1 },
      { name: `${TMP_PREFIX}running`, mtimeMs: now - 60_000 },
      { name: "workbench-e2e-other", mtimeMs: now - STALE_AFTER_MS * 10 },
      { name: "npm-cache", mtimeMs: 0 },
    ];
    expect(staleWorkspaces(dirs, now)).toEqual([`${TMP_PREFIX}old`]);
  });
});

describe("pruneStaleWorkspaces", () => {
  it("sweeps a crashed run's leftovers and leaves a live run alone", () => {
    const dead = makeDir(`${TMP_PREFIX}dead`);
    const deadProjects = makeDir(`${TMP_PREFIX}dead-projects`);
    ageBy(deadProjects, STALE_AFTER_MS * 2);
    ageBy(dead, STALE_AFTER_MS * 2);
    const live = makeDir(`${TMP_PREFIX}live`);
    const stranger = makeDir("someone-elses-temp");
    ageBy(stranger, STALE_AFTER_MS * 100);

    const removed = pruneStaleWorkspaces(tmp);

    expect(removed).toContain(dead);
    expect(removed).toContain(deadProjects);
    expect(fs.existsSync(dead)).toBe(false);
    expect(fs.existsSync(deadProjects)).toBe(false);
    expect(fs.existsSync(live)).toBe(true);
    expect(fs.existsSync(stranger)).toBe(true);
  });

  it("says nothing about a temp directory that is not there", () => {
    expect(pruneStaleWorkspaces(path.join(tmp, "no-such-dir"))).toEqual([]);
  });
});

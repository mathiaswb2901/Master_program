/**
 * Budget — twenty file changes must not cost a single workspace listing.
 *
 * **This was an expected failure, and is not any more.** PR #35 shipped it as
 * `test.fail()` — green while the app did the wrong thing, red the moment it
 * was fixed — precisely so the defect could not be forgotten and so the fix
 * would arrive with its regression test already written. Converting it into an
 * ordinary passing assertion is what this PR is for.
 *
 * What it used to measure: the app answered every `file_changed` event by
 * re-fetching the whole tree (`store.ts` -> `refreshTree`, debounced 500 ms).
 * On the 5,005-file fixture one such fetch is 16 directory listings, ~460 ms of
 * endpoint time and **471 KB of JSON**. Measured 2026-08-05: twenty changes
 * produced **20 full tree fetches and 9,446 KB** — an agent making twenty
 * edits, or a `git checkout`, cost the machine twenty complete walks and 9.4 MB
 * of JSON to move twenty rows that mostly did not change.
 *
 * What it measures now: the client patches its own tree from the event it
 * already receives, so twenty changes cost **zero** requests of either kind.
 * Both endpoints are counted, not just the old one — a fix that replaced one
 * refetch with another would be no fix at all.
 *
 * The assertion counts *requests*, not milliseconds, so it blocks in CI. It is
 * also implementation-agnostic: the wait is on a new row appearing in the tree,
 * which any correct implementation must still do.
 */

import { expect, test } from "@playwright/test";

import { removeFixtureFile, writeFixtureFile } from "./fixture";
import { record, round } from "./instrument";

/** Twenty single-file changes: the size of one agent turn, or one small rebase. */
const CHANGES = 20;
const PREFIX = "perf-watch-";

const created = Array.from({ length: CHANGES }, (_, i) => `${PREFIX}${String(i).padStart(2, "0")}.txt`);

test.afterAll(() => {
  // The fixture is fresh per run, but a developer pointing WB_PERF_WORKSPACE at
  // a stable directory must not accumulate these.
  for (const name of created) removeFixtureFile(name);
});

test("twenty file changes cost no workspace listings", async ({ page }, info) => {
  /** Every way the app can ask the server what the workspace contains: the
   * recursive walk (`/tree`, the QuickBar's search index) and the per-directory
   * listing (`/dir`, what the tree renders from). */
  const listings: { url: string; bytes: number; ms: number }[] = [];
  page.on("response", async (response) => {
    const url = response.url();
    if (!url.includes("/api/files/tree") && !url.includes("/api/files/dir")) return;
    const timing = response.request().timing();
    listings.push({
      url,
      bytes: Number(response.headers()["content-length"] ?? 0),
      ms: Math.max(0, timing.responseEnd - timing.requestStart),
    });
  });

  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "notes.md", exact: true })).toBeVisible();

  // The load's own listing is legitimate; the budget is about what the
  // *changes* cost after it.
  const atLoad = listings.length;
  const bytesAtLoad = listings.reduce((total, r) => total + r.bytes, 0);
  const msAtLoad = listings.reduce((total, r) => total + r.ms, 0);

  for (const name of created) {
    writeFixtureFile(name, `${name}\n`);
    // Wait on the app's own signal — the row — not on a timer. Any
    // implementation that keeps the tree correct must produce it.
    await expect(page.getByRole("treeitem", { name, exact: true })).toBeVisible();
  }

  const after = listings.slice(atLoad);
  const bytes = listings.reduce((total, r) => total + r.bytes, 0) - bytesAtLoad;
  const ms = listings.reduce((total, r) => total + r.ms, 0) - msAtLoad;
  await record(
    info,
    "watcher-churn",
    `${CHANGES} file changes caused ${after.length} workspace listings, ` +
      `${bytes} bytes, ${round(ms)} ms of endpoint time ` +
      `(launch cost ${atLoad} listing(s), ${bytesAtLoad} bytes, ${round(msAtLoad)} ms)`,
    {
      changes: CHANGES,
      listingsAfterLoad: after.length,
      bytesAfterLoad: bytes,
      endpointMsAfterLoad: ms,
      atLoad: { listings: atLoad, bytes: bytesAtLoad, endpointMs: msAtLoad },
      urlsAfterLoad: after.map((r) => r.url),
    },
  );

  expect(after.map((r) => r.url), `${CHANGES} changes should cost no listings`).toEqual([]);
  // The launch itself must stay cheap too: one `scandir` of the root, not a
  // walk. A regression that put the search index back on the startup path
  // would leave the count above at zero and still be the old behaviour.
  expect(atLoad, "launch should read one directory").toBe(1);
});

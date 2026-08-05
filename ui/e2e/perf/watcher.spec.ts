/**
 * Budget — twenty file changes must not cost twenty full workspace walks.
 *
 * **This test is expected to fail today.** `test.fail()` is Playwright's xfail:
 * the run is green while the app still does the wrong thing, and it turns *red*
 * the moment the behaviour is fixed and the annotation is not removed. That is
 * the property worth having — a known defect that cannot be forgotten, and that
 * converts into an ordinary passing regression test in the PR that fixes it.
 *
 * What it is measuring: the app answers every `file_changed` event by
 * re-fetching the whole tree (`store.ts` -> `refreshTree`, debounced 500 ms).
 * On the 5,005-file fixture one such fetch is 16 directory listings, ~460 ms of
 * endpoint time and **471 KB of JSON**. Measured here 2026-08-05: twenty
 * changes produced **20 full tree fetches and 9,443 KB** — so an agent making
 * twenty edits, or a `git checkout`, costs the machine twenty complete walks
 * and 9.4 MB of JSON to move twenty rows that mostly did not change. That is
 * the "old sluggish feel" as a number.
 *
 * The fix is not in this PR (it changes the file-change protocol: an
 * incremental tree patch on `/ws/events`, or a tree endpoint that answers from
 * a maintained index). It is the Feel track's next item, and this test is its
 * acceptance criterion — see ROADMAP.md, "Feel track".
 *
 * The assertion counts *requests*, not milliseconds, so it blocks in CI. It is
 * also implementation-agnostic: the wait is on a new row appearing in the tree,
 * which any correct implementation must still do.
 */

import { expect, test } from "@playwright/test";

import { removeFixtureFile, writeFixtureFile } from "./fixture";
import { record } from "./instrument";

/** Twenty single-file changes: the size of one agent turn, or one small rebase. */
const CHANGES = 20;
const PREFIX = "perf-watch-";

const created = Array.from({ length: CHANGES }, (_, i) => `${PREFIX}${String(i).padStart(2, "0")}.txt`);

test.afterAll(() => {
  // The fixture is fresh per run, but a developer pointing WB_PERF_WORKSPACE at
  // a stable directory must not accumulate these.
  for (const name of created) removeFixtureFile(name);
});

test.fail(); // xfail — see the file comment. Remove this line with the fix.
test("twenty file changes cost no full tree walks", async ({ page }, info) => {
  const treeRequests: string[] = [];
  let treeBytes = 0;
  page.on("response", (response) => {
    if (!response.url().includes("/api/files/tree")) return;
    treeRequests.push(response.url());
    treeBytes += Number(response.headers()["content-length"] ?? 0);
  });

  await page.goto("/");
  await expect(page.getByRole("treeitem", { name: "notes.md", exact: true })).toBeVisible();

  // The load's own tree fetch is legitimate; the budget is about what the
  // *changes* cost after it.
  const afterLoad = treeRequests.length;
  const bytesAfterLoad = treeBytes;

  for (const name of created) {
    writeFixtureFile(name, `${name}\n`);
    // Wait on the app's own signal — the row — not on a timer. Any
    // implementation that keeps the tree correct must produce it.
    await expect(page.getByRole("treeitem", { name, exact: true })).toBeVisible();
  }

  const walks = treeRequests.length - afterLoad;
  const bytes = treeBytes - bytesAfterLoad;
  await record(
    info,
    "watcher-churn",
    `${CHANGES} file changes caused ${walks} full tree fetches, ${Math.round(bytes / 1024)} KB`,
    { changes: CHANGES, treeFetchesAfterLoad: walks, bytesAfterLoad: bytes },
  );

  expect(walks, `${CHANGES} changes should cost no full walks`).toBe(0);
});

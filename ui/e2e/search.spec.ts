/**
 * Journey — workspace content search (M7 V7b, Ctrl+Shift+F).
 *
 * The order is the feature. The chord opens the panel; a query surfaces hits
 * across more than one file, grouped by file; clicking a hit opens that file in
 * the editor. And the one invariant that is easy to get wrong: a query never
 * returns a match that lives inside a build cache the file tree hides — the
 * seeded `desktop/src-tauri/target` carries `CACHEDIR.TAG`, and the text only in
 * it (`allow-popup`) must come back with nothing.
 *
 * The seeded workspace (`ui/e2e/workspace.ts`) carries `SE3` in two visible
 * files — `src/model.py` (`PRICE_AREA = 'SE3'`) and `notes.md` (`SE3 battery
 * notes`) — which is the multi-file query this drives.
 */

import { expect, test } from "@playwright/test";

import { openApp } from "./app";

test("content search: the chord opens it, a query finds hits across files, a hit opens the editor", async ({
  page,
}) => {
  await openApp(page);

  await test.step("Ctrl+Shift+F opens the search panel", async () => {
    await page.keyboard.press("Control+Shift+F");
    await expect(page.locator(".wb-search")).toBeVisible();
    await expect(page.locator(".wb-search-input")).toBeFocused();
  });

  await test.step("a query surfaces hits grouped across more than one file", async () => {
    await page.locator(".wb-search-input").fill("SE3");
    await page.locator(".wb-search-input").press("Enter");
    // Two visible files carry SE3; the results group by file.
    await expect(page.locator('.wb-search-file[data-path="src/model.py"]')).toBeVisible();
    await expect(page.locator('.wb-search-file[data-path="notes.md"]')).toBeVisible();
    // The matching line is shown, with the match marked.
    const modelHit = page.locator('.wb-search-file[data-path="src/model.py"] .wb-search-hit');
    await expect(modelHit.first()).toContainText("PRICE_AREA");
    await expect(modelHit.first().locator(".wb-search-mark")).toContainText("SE3");
  });

  await test.step("clicking a hit opens that file in the editor", async () => {
    await page
      .locator('.wb-search-file[data-path="src/model.py"] .wb-search-hit')
      .first()
      .click();
    // The file opens as an editor tab, and the Monaco surface mounts.
    await expect(
      page.locator('.wb-editor-tab.is-active', { hasText: "model.py" }),
    ).toBeVisible();
    await expect(page.locator(".wb-editor-body .monaco-editor").first()).toBeVisible();
  });

  await test.step("a match inside a CACHEDIR.TAG'd build cache is never returned", async () => {
    await page.locator(".wb-search-input").fill("allow-popup");
    await page.locator(".wb-search-input").press("Enter");
    // The only file carrying `allow-popup` sits under `desktop/src-tauri/target`,
    // which is tagged as a build cache — the tree hides it, and so does search.
    await expect(page.locator(".wb-search-none")).toContainText("No matches");
    await expect(page.locator(".wb-search-file")).toHaveCount(0);
  });
});

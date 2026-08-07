/**
 * Journey 11 — every Claude conversation, grouped by folder.
 *
 * The owner's ask, verbatim: *"For Claude I also need to have an overview like
 * we do in Claude Code with which chats belong to which folder."* This journey
 * is that sentence, driven: open the browser, see the folders, find the one
 * conversation by searching for it three "days" later, and land in it.
 *
 * Asserts, in order:
 *  - the QuickBar command opens the panel, and the seeded store renders as
 *    folder groups with per-row titles, turn counts and ages;
 *  - a transcript that will not parse keeps its row and says it is unreadable —
 *    a conversation that exists is never silently dropped;
 *  - a folder outside this workspace is **shown and refused with a reason**
 *    (half B lifts that jail; half A must not hide the conversation);
 *  - search filters across titles *and* folder names;
 *  - clicking a row resumes it into an agent pane bound to that session, with
 *    what was said before already in the chat;
 *  - clicking the same row again focuses that pane rather than cloning it —
 *    the pane-identity rule, exercised the way a user hits it;
 *  - two browser panes are two independent browsers (one scoped to a folder,
 *    one not) **through a reload**, which is the plural-tool requirement in
 *    CLAUDE.md.
 *
 * It ends by resetting the arrangement, because the journeys after it share
 * this workspace and expect the window they have always had.
 */

import path from "node:path";

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp, treeItem, typeInEditor } from "./app";
import {
  CONV_GONE_KEY,
  CONV_GONE_TITLE,
  CONV_OUTSIDE_TITLE,
  CONV_ROOT_OTHER_TITLE,
  CONV_ROOT_TITLE,
  CONV_SRC_PROJECT_KEY,
  CONV_SRC_REPLY,
  CONV_SRC_TITLE,
  E2E_WORKSPACE,
  NOTES_FILE,
  OUTSIDE_FILE,
  OUTSIDE_WORKSPACE,
  removeOutsideWorkspace,
} from "./workspace";

const browser = (page: Page): Locator => page.locator(".wb-conv");
const rows = (page: Page): Locator => page.locator(".wb-conv-row");
const row = (page: Page, title: string): Locator =>
  page.locator(".wb-conv-row", { hasText: title }).first();
const groupNames = (page: Page): Locator => page.locator(".wb-conv-group-name");

/** Run a QuickBar command by its row title. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/**
 * Open the browser and wait for the seeded store to have landed.
 *
 * `toHaveCount(1)`, not `.first()`: running the command with the panel already
 * open must *focus* it, and the bug this caught did the opposite — a plural
 * tool's default pane is minted afresh by `openPanel`, so the second run of the
 * command left two identical browsers and two search boxes.
 */
async function openBrowser(page: Page): Promise<void> {
  await runCommand(page, "Browse Claude conversations");
  await expect(browser(page)).toHaveCount(1);
  await expect(row(page, CONV_ROOT_TITLE)).toBeVisible();
  await runCommand(page, "Browse Claude conversations");
  await expect(browser(page)).toHaveCount(1);
}

/** Pane ids as they are on disk — the strings the next launch will trust. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** Wait until the debounced autosave has written a pane we just made. */
async function persisted(page: Page, paneId: string): Promise<void> {
  await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toContain(paneId);
}

const NO_LAYOUT = { current: null, current_name: null, saved: [] };

/** Each test starts from the default arrangement. The window autosaves, and
 * these four tests share one workspace — without this, the pane test 4 opens is
 * scenery in test 1, and "the search box" stops being one element. */
test.beforeEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  // The switch-and-open test re-roots the backend into the outside folder; every
  // journey after this one expects the seeded workspace, so put it back first —
  // then remove the outside folder so its now-stale recents entry cannot leak a
  // dynamic "Open workspace …" command into a later journey — and reset the
  // layout in the workspace it belongs to.
  await context.post("/api/workspace/switch", { data: { path: E2E_WORKSPACE } });
  removeOutsideWorkspace();
  await context.put("/api/layouts", { data: NO_LAYOUT });
  await context.dispose();
});

test.describe.serial("the conversation browser", () => {
  test("shows every conversation grouped by folder, and says what it cannot open", async ({
    page,
  }) => {
    await openApp(page);
    await openBrowser(page);

    await test.step("the folders are the groups", async () => {
      const names = await groupNames(page).allTextContents();
      // The workspace root and `src` are inside this workspace and named as
      // such; the two that are not keep a name of their own.
      expect(names).toContain("workspace root");
      expect(names).toContain("src");
      expect(names).toContain(CONV_GONE_KEY);
      expect(names.length).toBeGreaterThanOrEqual(4);
    });

    await test.step("a row carries the title, the turn count and the age", async () => {
      const first = row(page, CONV_ROOT_TITLE);
      await expect(first).toBeVisible();
      // Two user messages carried text; the tool result between them did not,
      // and Claude Code stores those as `user` records too.
      await expect(first).toContainText("2 turns");
      // "now", "3m", "2h", "5d" — the shape, not a value that depends on how
      // long the suite took to get here.
      await expect(first.locator(".wb-conv-time")).toHaveText(/^(now|\d+[mhd])$/);
    });

    await test.step("a transcript that will not parse keeps its row", async () => {
      await expect(page.locator(".wb-conv-problem").first()).toBeVisible();
      await expect(row(page, "(unreadable transcript)")).toBeVisible();
    });

    await test.step("a folder outside this workspace is shown, and offers to switch to it", async () => {
      const outside = row(page, CONV_OUTSIDE_TITLE);
      await expect(outside).toBeVisible();
      // Half B: not a dead end. The row is actionable (it switches the workspace
      // on the way in) rather than blocked, and it says so in words (§7).
      await expect(outside).toHaveClass(/is-switch/);
      await expect(outside).not.toHaveClass(/is-blocked/);
      await expect(outside.locator(".wb-conv-switch")).toBeVisible();
      // The reason is on the folder, in words, next to the conversation.
      const reasons = await page.locator(".wb-conv-reason").allTextContents();
      expect(reasons.join(" ")).toContain("Outside this workspace");
    });

    await test.step("a folder that no longer exists shows the key that was stored", async () => {
      const gone = row(page, CONV_GONE_TITLE);
      await expect(gone).toBeVisible();
      await expect(gone).toHaveClass(/is-blocked/);
      const reasons = await page.locator(".wb-conv-reason").allTextContents();
      expect(reasons.join(" ")).toContain("moved or deleted");
    });

    // Found by eye against the author's real store, where the panel opened into
    // a 90px column and the rows pushed it into a sideways scroll: a folder
    // label is a whole absolute path and a title is a sentence, so this panel
    // is the one most likely to do it. A dock column dragged narrow must clip.
    await test.step("never scrolls sideways, however narrow its column gets", async () => {
      const sideways = (): Promise<boolean> =>
        page
          .locator(".wb-conv-list")
          .first()
          .evaluate((el) => el.scrollWidth > el.clientWidth);
      expect(await sideways()).toBe(false);
      await page.setViewportSize({ width: 640, height: 720 });
      await expect(row(page, CONV_ROOT_TITLE)).toBeVisible();
      expect(await sideways()).toBe(false);
      await page.setViewportSize({ width: 1280, height: 720 });
    });

    await test.step("clicking one whose folder is gone says why, and opens nothing", async () => {
      // The gone folder is the case half B leaves refused: there is no directory
      // to switch to, so the click surfaces the reason and opens nothing.
      const panes = await page.locator(".dv-groupview").count();
      await row(page, CONV_GONE_TITLE).click();
      await expect(
        page.locator(".wb-toast.is-warn").filter({ hasText: "moved or deleted" }),
      ).toBeVisible();
      await expect(page.locator(".dv-groupview")).toHaveCount(panes);
    });
  });

  test("finds a conversation by searching, across titles and folders", async ({ page }) => {
    await openApp(page);
    await openBrowser(page);
    const search = page.getByRole("searchbox", { name: "Search conversations" });

    await test.step("by something said in it", async () => {
      await search.fill("DST");
      await expect(rows(page)).toHaveCount(1);
      await expect(row(page, CONV_ROOT_TITLE)).toBeVisible();
    });

    await test.step("by the folder it ran in", async () => {
      await search.fill("src");
      await expect(row(page, CONV_SRC_TITLE)).toBeVisible();
      await expect(page.locator(".wb-conv-row", { hasText: CONV_ROOT_OTHER_TITLE })).toHaveCount(
        0,
      );
    });

    await test.step("and says so when nothing matches", async () => {
      await search.fill("nothing whatsoever matches this");
      await expect(rows(page)).toHaveCount(0);
      await expect(browser(page)).toContainText("No conversation matches");
    });
  });

  test("opens a row into an agent pane, and never a second one", async ({ page }) => {
    await openApp(page);
    await openBrowser(page);

    await row(page, CONV_SRC_TITLE).click();

    await test.step("the conversation is live in its own pane, with its history", async () => {
      // The pane's tab is the conversation, which is the whole point of binding
      // a pane to a session id rather than to "the agent panel".
      await expect(
        page.locator(".wb-panel-tab", { hasText: CONV_SRC_TITLE }).first(),
      ).toBeVisible();
      // What was said before is there — a resume that showed an empty chat over
      // a transcript that exists would be the wrong kind of honest.
      await expect(
        page.locator(".wb-msg-user", { hasText: CONV_SRC_TITLE }).first(),
      ).toBeVisible();
      await expect(page.getByText(CONV_SRC_REPLY).first()).toBeVisible();
      // Live, not a tombstone: the pane found its session in the listing.
      await expect(page.locator(".wb-pane-note")).toHaveCount(0);
    });

    await test.step("opening it again focuses that pane rather than cloning it", async () => {
      const panes = await page.locator(".dv-groupview").count();
      // Back to the browser pane, then the same row again.
      await page.locator(".wb-panel-tab", { hasText: "Conversations" }).first().click();
      await row(page, CONV_SRC_TITLE).click();
      await expect(page.locator(".dv-groupview")).toHaveCount(panes);
      await expect(page.locator(".wb-panel-tab", { hasText: CONV_SRC_TITLE })).toHaveCount(1);
    });
  });

  test("two browsers are two browsers, through a reload", async ({ page }) => {
    await openApp(page);
    await openBrowser(page);

    await test.step("a second pane, scoped to one folder", async () => {
      await page.keyboard.press("Alt+S");
      const picker = page.getByRole("dialog", { name: "Split this pane to the right" });
      await expect(picker).toBeVisible();
      await picker.locator(".wb-qb-row", { hasText: "Conversations in src" }).first().click();
      await expect(picker).toBeHidden();
      await expect(browser(page)).toHaveCount(2);
    });

    const scoped = browser(page).filter({ hasText: CONV_SRC_TITLE }).last();

    await test.step("the scoped one shows one folder; the other still shows them all", async () => {
      // Scoped: `src` only, so the root conversation is not in it.
      await expect(scoped.locator(".wb-conv-group")).toHaveCount(1);
      await expect(scoped.locator(".wb-conv-row", { hasText: CONV_ROOT_TITLE })).toHaveCount(0);
      // Unscoped: everything, including the root conversation.
      await expect(page.locator(".wb-conv-row", { hasText: CONV_ROOT_TITLE })).toHaveCount(1);
    });

    await test.step("their searches are their own", async () => {
      await scoped.getByRole("searchbox", { name: "Search conversations" }).fill("bid");
      await expect(scoped.locator(".wb-conv-row")).toHaveCount(1);
      // The other pane's list is untouched — a shared query would be one
      // browser wearing two tabs.
      await expect(page.locator(".wb-conv-row", { hasText: CONV_ROOT_TITLE })).toHaveCount(1);
    });

    await test.step("a reload brings back both, still pointed where they were", async () => {
      // The pane id is the whole of what a restart gets back (`ui/src/panes.ts`),
      // so the scope is only really persisted if this exact string is on disk.
      await persisted(page, `conversations#${CONV_SRC_PROJECT_KEY}`);
      await page.reload();
      await expect(browser(page)).toHaveCount(2);
      const restored = browser(page).filter({ hasText: CONV_SRC_TITLE }).last();
      await expect(restored.locator(".wb-conv-group")).toHaveCount(1);
      await expect(page.locator(".wb-conv-row", { hasText: CONV_ROOT_TITLE })).toHaveCount(1);
    });
  });

  /**
   * Half B (M5 item 12) — opening a conversation whose folder is outside the
   * workspace, by switching the workspace to it first.
   *
   * Runs last, because it re-roots the backend; the `afterAll` puts it back. It
   * asserts the three things the half is: the switch is offered (never bypassed
   * — a dirty buffer stops it exactly as a plain switch would), the switch that
   * lands carries the conversation's history into a live pane, and opening the
   * same row twice focuses the one pane rather than cloning it.
   */
  test("opens a conversation outside the workspace by switching to its folder", async ({
    page,
  }) => {
    await openApp(page);
    await openBrowser(page);

    const chipLabel = page.locator(".wb-workspace-chip-label");
    const firstName = path.basename(E2E_WORKSPACE);
    const outsideName = path.basename(OUTSIDE_WORKSPACE);
    const agentTab = page.locator(".wb-panel-tab", { hasText: CONV_OUTSIDE_TITLE });
    await expect(chipLabel).toHaveText(firstName);

    await test.step("a dirty buffer blocks the switch, and nothing resumes", async () => {
      await treeItem(page, NOTES_FILE).click();
      await typeInEditor(page, "unsaved edit\n");
      await expect(page.locator(".wb-editor-tab.is-dirty")).toHaveCount(1);

      await row(page, CONV_OUTSIDE_TITLE).click();
      const confirm = page.getByRole("dialog", { name: "Switch workspace?" });
      await expect(confirm).toBeVisible();
      // The same guard a plain switch runs — the buffer is named, and Cancel
      // really cancels: still here, and no pane was opened behind the prompt.
      await expect(confirm).toContainText(NOTES_FILE);
      await confirm.getByRole("button", { name: "Cancel" }).click();
      await expect(chipLabel).toHaveText(firstName);
      await expect(agentTab).toHaveCount(0);
    });

    await test.step("discarding it, the switch lands and the conversation resumes with its history", async () => {
      await row(page, CONV_OUTSIDE_TITLE).click();
      const confirm = page.getByRole("dialog", { name: "Switch workspace?" });
      await expect(confirm).toBeVisible();
      await confirm.getByRole("button", { name: "Discard changes" }).click();

      // The window re-rooted through the switcher's own flow: the chip and the
      // tree are the other project's now.
      await expect(chipLabel).toHaveText(outsideName);
      await expect(treeItem(page, OUTSIDE_FILE)).toBeVisible();
      await expect(treeItem(page, NOTES_FILE)).toHaveCount(0);

      // And the conversation is live in its own pane, carrying what was said
      // before — a resume, not an empty chat over a transcript that exists.
      await expect(agentTab.first()).toBeVisible();
      await expect(
        page.locator(".wb-msg-user", { hasText: CONV_OUTSIDE_TITLE }).first(),
      ).toBeVisible();
      await expect(page.getByText("Picking up where we left off.").first()).toBeVisible();
      await expect(page.locator(".wb-pane-note")).toHaveCount(0);
    });

    await test.step("opening the same row again focuses its pane rather than cloning it", async () => {
      // Now inside its workspace, so this is the plain focus rule: the row is a
      // live in-workspace conversation and clicking it reveals the one pane.
      await openBrowser(page);
      const panes = await page.locator(".dv-groupview").count();
      await row(page, CONV_OUTSIDE_TITLE).click();
      await expect(page.locator(".dv-groupview")).toHaveCount(panes);
      await expect(agentTab).toHaveCount(1);
    });
  });
});

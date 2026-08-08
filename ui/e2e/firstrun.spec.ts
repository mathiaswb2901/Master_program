/**
 * Journey 13 — first run / Setup: the app greets a fresh launch, says honestly
 * what is connected, and then gets out of the way (M7 §2).
 *
 * The welcome card (journey 12) teaches *the window*; Setup teaches *the
 * connections*. Everything below is the ROADMAP's first-run exit criterion
 * turned into assertions a headless build can fail on:
 *
 *  - a window nobody has arranged **opens the Setup walkthrough**, a tab beside
 *    the welcome card — not a modal; nothing has to be dismissed to use the app;
 *  - each check renders its **honest state**: under the fake agent Claude is
 *    signed out, so its row is *action needed* and carries the exact command
 *    `claude /login` as copyable text — never a button that logs in;
 *  - it **teaches** by pointing at the window basics, not duplicating them;
 *  - **dismissal is workspace state** (`.workbench/setup.json`): "Got it" retires
 *    the tab and a relaunch does not bring it back — it never nags again;
 *  - a window that **already has `.workbench` state** (the state every other
 *    journey runs in) shows no walkthrough at all.
 *
 * The workspace is seeded **dismissed** (`e2e/workspace.ts`); this journey clears
 * that through the app's own API to get first run back, and puts it as it found
 * it, so it holds whatever order the suite runs in — the discover journey's
 * convention exactly.
 */

import { expect, request, test, type Page } from "@playwright/test";

import { launchSettled, openApp, workspaceReady } from "./app";
import { SETUP_FILE } from "./workspace";

const setup = (page: Page) => page.getByRole("region", { name: "Set up Workbench" });

async function writeSetup(page: Page, dismissed: boolean): Promise<void> {
  const response = await page.request.put("/api/files/content", {
    data: { path: SETUP_FILE, content: `${JSON.stringify({ dismissed }, null, 2)}\n` },
  });
  expect(response.ok(), "the setup file was written").toBe(true);
}

async function readSetupFile(page: Page): Promise<{ dismissed?: unknown }> {
  const response = await page.request.get(
    `/api/files/content?path=${encodeURIComponent(SETUP_FILE)}`,
  );
  expect(response.ok(), "the setup file is readable").toBe(true);
  const body = (await response.json()) as { content: string };
  return JSON.parse(body.content) as { dismissed?: unknown };
}

/** No saved arrangement, so nothing restores over the auto-opened panel — the
 * half of the auto-open rule that is not about the setup file. */
async function clearLayouts(page: Page): Promise<void> {
  await page.request.put("/api/layouts", {
    data: { current: null, current_name: null, saved: [] },
  });
}

async function relaunch(page: Page): Promise<void> {
  await page.reload();
  await workspaceReady(page);
  await launchSettled(page);
}

/**
 * A window nobody has arranged and that has not answered Setup.
 *
 * Two reloads on purpose. The suite shares one workspace, so by the time this
 * journey runs the earlier journeys have left an arrangement on disk — and the
 * live page carries an *armed, debounced* layout write. Clearing the file before
 * that write lands lets it re-appear (a non-null `current`), and the auto-open
 * rule skips a window that already has a saved arrangement. So: reload onto a
 * clean page first (letting any pending write settle), *then* clear the file and
 * reload again — the final page reads a genuinely empty arrangement, the one
 * state the auto-open is about.
 */
async function firstRun(page: Page): Promise<void> {
  await writeSetup(page, false);
  await relaunch(page);
  await clearLayouts(page);
  await relaunch(page);
}

/** Put the workspace back the way the rest of the suite expects it: used before,
 * and unarranged. A Setup tab left behind would fail the panel-counting journeys. */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.put("/api/files/content", {
    data: { path: SETUP_FILE, content: `${JSON.stringify({ dismissed: true }, null, 2)}\n` },
  });
  await context.dispose();
});

test("a fresh launch greets you, says what is connected, and then gets out of the way", async ({
  page,
}) => {
  await openApp(page);

  await test.step("a workspace that already has .workbench shows no walkthrough", async () => {
    // The state every other journey runs in, asserted rather than assumed. This
    // is what "never sees it again" has to mean on a used workspace.
    await expect(setup(page)).toBeHidden();
  });

  await test.step("a window nobody has arranged opens the Setup walkthrough", async () => {
    await firstRun(page);
    await expect(setup(page)).toBeVisible();
    // A tab, not a modal: the workspace tree is right there beside it.
    await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();
    await expect(setup(page).getByRole("heading", { name: "Let's get you connected" })).toBeVisible();
  });

  await test.step("each check renders its honest state", async () => {
    // Every connection has a row. The ids are stable; the titles are what a
    // human reads.
    for (const title of ["Claude", "Office", "OnlyOffice", "Workspace"]) {
      await expect(
        setup(page).locator(".wb-setup-row-title", { hasText: new RegExp(`^${title}$`) }),
      ).toBeVisible();
    }
    // Under the fake agent there is no real Claude, so it is signed out —
    // action needed, with the exact command as copyable text, never a button.
    const claude = setup(page).locator(".wb-setup-row.is-action_needed", { hasText: "Claude" });
    await expect(claude).toBeVisible();
    await expect(claude.locator(".wb-setup-instruction")).toHaveText("claude /login");
    // And nowhere in the walkthrough is there a control that signs you in.
    await expect(setup(page).getByRole("button", { name: /sign in/i })).toHaveCount(0);
  });

  await test.step("the status bar carries the unanswered reading", async () => {
    // The quiet-bar chip, present while the walkthrough is unanswered and
    // something needs action; it retires with the walkthrough below.
    await expect(page.locator(".wb-setup-chip")).toBeVisible();
  });

  await test.step("it teaches the window without duplicating the welcome card", async () => {
    await expect(setup(page).getByText("Learn the window")).toBeVisible();
    await expect(
      setup(page).getByRole("button", { name: "Show the welcome card" }),
    ).toBeVisible();
    await expect(setup(page).getByRole("button", { name: "Keyboard shortcuts" })).toBeVisible();
  });

  await test.step("Got it retires the tab and persists the dismissal", async () => {
    await setup(page).getByRole("button", { name: "Got it" }).click();
    await expect(setup(page)).toBeHidden();
    // The chip retires with it.
    await expect(page.locator(".wb-setup-chip")).toBeHidden();
    // Dismissal is workspace state, written through the files API.
    expect((await readSetupFile(page)).dismissed).toBe(true);
  });

  await test.step("a relaunch does not bring it back — it never nags again", async () => {
    // Clear the arrangement first, so the *only* thing that could reopen the
    // walkthrough is the auto-open — and the persisted dismissal is what keeps
    // it shut. (Without this the test races the debounced layout write that
    // removes the just-closed pane; a saved arrangement still carrying it would
    // restore the pane by the ordinary layout mechanism, which is not what
    // "never nags" is about — that is the dismissal flag, asserted here.)
    await clearLayouts(page);
    await relaunch(page);
    await expect(setup(page)).toBeHidden();
    await expect(page.locator(".wb-setup-chip")).toBeHidden();
  });
});

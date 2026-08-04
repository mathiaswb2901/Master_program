/**
 * Journey 6 — status bar and the attention badge.
 *
 * Asserts:
 *  - while a turn is running the status bar carries a live session chip in the
 *    working state (the fake agent holds one turn open long enough to observe
 *    it), and returns to idle when the turn ends;
 *  - a permission request puts the attention prefix on `document.title` — the
 *    signal a user gets when the window is not focused — and answering the
 *    card in the UI clears it and unblocks the agent, which echoes the answer.
 */

import { expect, test } from "@playwright/test";

import { assistantBlocks, newSession, openApp, sendChat } from "./app";

const BASE_TITLE = "Workbench";
const ATTENTION_TITLE = `● ${BASE_TITLE}`;

test("working chip during a turn, attention badge on a permission prompt", async ({ page }) => {
  await openApp(page);
  await newSession(page);

  await test.step("the session chip shows working while the turn runs", async () => {
    // Scoped by state, not by count: earlier journeys leave their own (idle)
    // sessions live on the shared backend, and each gets a chip too.
    const working = page.getByRole("img", { name: "Working", exact: true });
    const workingChip = page.locator(".wb-status-chip").filter({ has: working });
    const workingCount = page.locator(".wb-status-count").filter({ has: working });

    await sendChat(page, "stay busy for a moment");
    await expect(workingChip).toHaveCount(1);
    await expect(workingCount).toContainText("1");
    // ...and it settles on its own when the turn finishes.
    await expect(workingChip).toHaveCount(0);
    await expect(page.locator(".wb-chat-header .wb-badge")).toContainText("Idle");
  });

  await test.step("a permission request badges the window title", async () => {
    expect(await page.title()).toBe(BASE_TITLE);
    await sendChat(page, "ask permission before continuing");
    const card = page.locator(".wb-perm-card");
    await expect(card).toContainText("echo scripted-permission");
    await expect.poll(() => page.title()).toBe(ATTENTION_TITLE);
  });

  await test.step("answering the card clears the badge and unblocks the agent", async () => {
    await page.locator(".wb-perm-card").getByRole("button", { name: "Allow" }).click();
    await expect(page.locator(".wb-perm-decision")).toHaveText("Allowed");
    await expect(assistantBlocks(page).last()).toContainText("permission: allowed");
    await expect.poll(() => page.title()).toBe(BASE_TITLE);
  });
});

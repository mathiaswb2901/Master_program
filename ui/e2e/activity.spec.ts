/**
 * Journey — live agent activity: "see everywhere Claude is editing".
 *
 * The claim under test is *reach*, and the order of the steps is the argument.
 * A tool call announces itself on the socket belonging to one conversation; this
 * panel sees it because the server republishes a bounded row on the shared bus.
 * So the journey opens the panel, then drives sessions, and asserts the panel —
 * never the chat.
 *
 * The `slow tool` trigger exists for the middle of this: it holds the turn
 * *between* the announcement and the result, which is the only way a browser can
 * be pointed at a call that is genuinely in flight. Everything else here is what
 * a real turn reaches — a row appearing, a line replaced in place when it
 * settles, a file target that opens the file, and a second session changing at
 * its own rhythm next to the first.
 *
 * **This journey sorts first** (Playwright loads specs in path order, and the
 * suite runs serially against one backend), so the fleet it opens on really has
 * never held a session — which is what makes the empty state assertable here at
 * all. If a spec ever sorts before `activity.spec.ts`, that first step is the
 * one to move; the other states are covered in `ActivityPanel.test.tsx`, where
 * four concurrent sessions can be staged in a millisecond.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import { newSession, openApp, sendChat } from "./app";
import { NOTES_FILE } from "./workspace";

/** The prompts, which are also the session titles the panel renders. */
const SLOW_PROMPT = "slow tool please, and take your time";
const SECOND_PROMPT = "use tool in a second conversation";
const STORM_PROMPT = "tool storm please, as loudly as you like";

/** Open the panel the way a user reaches a tool that is not on screen: the
 * QuickBar, through the registry. */
async function openActivityPanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">live agent activity");
  await quickbar.locator(".wb-qb-row", { hasText: "Show live agent activity" }).first().click();
  await expect(page.locator(".wb-activity")).toBeVisible();
}

/** One session's card, found by the title the panel renders for it. */
function card(page: Page, title: string): Locator {
  return page.locator(".wb-activity-session").filter({ hasText: title });
}

/**
 * Leave the workspace with the arrangement every other journey expects.
 *
 * Opening a panel changes the window, and the window is autosaved per workspace
 * — which this suite shares, serially, with this journey running first. The last
 * step closes the panel through the UI (that assertion is worth making); this
 * makes the reset unconditional, because the autosave is debounced and a page
 * torn down inside that window would hand `layout.spec.ts` a five-panel
 * arrangement. Same guard, same reason, as the layout journey's own.
 */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("live activity: the fleet, as it happens", async ({ page }) => {
  await openApp(page);
  await openActivityPanel(page);

  await test.step("an idle fleet says so on purpose", async () => {
    await expect(page.locator(".wb-activity")).toContainText("No agent sessions running");
    await expect(page.locator(".wb-activity-session")).toHaveCount(0);
    // §6.7: a quiet bar means nothing needs you.
    await expect(page.locator(".wb-activity-status")).toHaveCount(0);
  });

  await test.step("a session appears before it has done anything", async () => {
    await newSession(page);
    await expect(page.locator(".wb-activity-session")).toHaveCount(1);
    await expect(page.locator(".wb-activity")).toContainText("Nothing running");
    await expect(page.locator(".wb-activity")).toContainText("0 of 1 working");
  });

  await test.step("a call in flight is on screen while it is running", async () => {
    await sendChat(page, SLOW_PROMPT);
    const running = card(page, SLOW_PROMPT);
    await expect(running.locator(".wb-activity-now")).toContainText(`Read: ${NOTES_FILE}`);
    // Not settled yet: the "just did" line has nothing to say about this call.
    await expect(running.locator(".wb-activity-then")).toHaveCount(0);
    // …and the status bar has stopped being quiet.
    await expect(page.locator(".wb-activity-status")).toContainText("1");
  });

  await test.step("the line is replaced in place when the call settles", async () => {
    const settled = card(page, SLOW_PROMPT);
    await expect(settled.locator(".wb-activity-then")).toContainText(`Read: ${NOTES_FILE}`);
    await expect(settled.locator(".wb-activity-then")).toContainText("Done");
    // Replaced, not appended: "now" goes back to saying nothing is running.
    await expect(settled.locator(".wb-activity-now")).toContainText("Nothing running");
    // Quiet again, with nothing in flight anywhere.
    await expect(page.locator(".wb-activity-status")).toHaveCount(0);
  });

  await test.step("the file an agent touched opens from the row", async () => {
    await card(page, SLOW_PROMPT).locator(".wb-activity-target").first().click();
    await expect(page.locator(".wb-editor-tab").filter({ hasText: NOTES_FILE })).toBeVisible();
  });

  await test.step("a second session gets its own row, at its own rhythm", async () => {
    await newSession(page);
    await sendChat(page, SECOND_PROMPT);
    await expect(page.locator(".wb-activity-session")).toHaveCount(2);
    // Each row is about its own conversation: the second one's tool call did
    // not overwrite the first one's history.
    await expect(card(page, SECOND_PROMPT).locator(".wb-activity-then")).toContainText(
      `Read: ${NOTES_FILE}`,
    );
    await expect(card(page, SLOW_PROMPT).locator(".wb-activity-then")).toContainText("Done");
    // Most recently active first — the row that just changed is the top one.
    const titles = await page.locator(".wb-activity-session-title").allInnerTexts();
    expect(titles[0]).toContain(SECOND_PROMPT);
  });

  await test.step("a busy fleet does not drown the panel", async () => {
    // A Grep-heavy turn: 40 announce/settle pairs with nothing between them, in
    // a third conversation. The window is capped by construction, so the card
    // stays a fixed-size reading and says how much it dropped rather than
    // growing without limit.
    await newSession(page);
    await sendChat(page, STORM_PROMPT);
    const stormed = card(page, STORM_PROMPT);
    await expect(stormed).toContainText("dropped");
    await expect(stormed.locator(".wb-activity-then")).toContainText("Grep:");
    // The two rows that were already there are untouched by the noise.
    await expect(card(page, SLOW_PROMPT).locator(".wb-activity-then")).toContainText(
      `Read: ${NOTES_FILE}`,
    );
  });

  await test.step("it reads maximized as well as in a narrow dock", async () => {
    // Alt+M is focus mode (§6.9). The card grid reflows; every row stays on
    // screen and keeps its two lines.
    await page.locator(".wb-activity").click();
    await page.keyboard.press("Alt+m");
    const cards = page.locator(".wb-activity-session");
    await expect(cards).toHaveCount(3);
    for (let index = 0; index < 3; index += 1) {
      await expect(cards.nth(index)).toBeVisible();
      await expect(cards.nth(index).locator(".wb-activity-now")).toBeVisible();
    }
    await page.keyboard.press("Alt+m");
  });

  await test.step("the tab it arrived on is the way back out", async () => {
    // Its tab carries a close button because it is not in the startup layout
    // (`panelTabInfo`) — and closing it matters beyond the assertion: the dock
    // arrangement is persisted per workspace, and this suite shares one, so a
    // panel left open here is a panel the layout journey finds later.
    await page.getByRole("button", { name: "Close Activity" }).click();
    await expect(page.locator(".wb-activity")).toHaveCount(0);
    // The capability is still reachable — the QuickBar command opens it again.
    await openActivityPanel(page);
    await page.getByRole("button", { name: "Close Activity" }).click();
    await expect(page.locator(".wb-activity")).toHaveCount(0);
  });
});

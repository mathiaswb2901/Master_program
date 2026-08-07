/**
 * Item 15 (PR3) — detach a running session and reattach to it.
 *
 * The two prior PRs are the pieces: PR1 replays a live session's transcript on
 * reattach (no blank chat), PR2 the named-session store that lets a session be
 * remembered with no pane. This journey is the feature they add up to:
 *
 *  - **detach** a running session — its pane closes as a *view*, the session
 *    keeps running server-side, and it is recorded so it can be returned to;
 *  - **reattach** from the Detached list — a pane opens back onto it and its
 *    transcript hydrates from disk (PR1's path), even across a reload;
 *  - **plural**: two detached sessions come back independently through a
 *    save (detach) / reload / restore (reattach) round trip — a message typed in
 *    one lands only in one (CLAUDE.md product principle 4).
 *
 * The single-session test rides a *resumed* conversation, the same honest
 * vehicle `transcript-reload.spec.ts` uses: the fake agent writes no on-disk
 * transcript, so the history that must come back after a reload has to be one
 * that really exists on disk — a resumed session's fixture, read back through
 * `GET /api/agents/{id}/transcript`.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp, workspaceReady } from "./app";
import { CONV_SRC_REPLY, CONV_SRC_TITLE } from "./workspace";

const NO_LAYOUT = { current: null, current_name: null, saved: [] };

async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** The agent panes on disk — `agent#<id>` is the whole of what a reload restores. */
async function persistedAgentPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).filter((id) => id.startsWith("agent#"));
}

/** The Reattach button in the browser's Detached section. Located by role, not by
 * a pane-internal class. */
const reattachButtons = (page: Page): Locator => page.getByRole("button", { name: "Reattach" });

/** The dockview group whose tab carries this title — how a specific session pane
 * is addressed without an unscoped locator on a pane-internal class. */
function paneByTitle(page: Page, title: string): Locator {
  return page.locator(".dv-groupview", {
    has: page.locator(".wb-panel-tab", { hasText: title }),
  });
}

/** Split the focused pane to the right and put a new agent session in it. */
async function splitNewSession(page: Page): Promise<void> {
  await page.keyboard.press("Alt+S");
  const dialog = page.getByRole("dialog", { name: "Split this pane to the right" });
  await expect(dialog).toBeVisible();
  await dialog.locator(".wb-qb-row", { hasText: "New agent session" }).first().click();
  await expect(dialog).toBeHidden();
}

/** Send a message into the currently focused pane's chat. */
async function sendInActivePane(page: Page, text: string): Promise<void> {
  const input = page.locator(".dv-active-group .wb-chat-input textarea");
  await expect(input).toBeVisible();
  await input.fill(text);
  await input.press("Enter");
}

test.beforeEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: NO_LAYOUT });
  await context.dispose();
});

test("detach a running session, then reattach after a reload with its transcript", async ({
  page,
}) => {
  await openApp(page);

  await test.step("resume a conversation into a live pane, with its history", async () => {
    await runCommand(page, "Browse Claude conversations");
    await page.locator(".wb-conv-row", { hasText: CONV_SRC_TITLE }).first().click();
    const pane = page.locator(".wb-pane-single");
    await expect(pane.locator(".wb-msg-user", { hasText: CONV_SRC_TITLE }).first()).toBeVisible();
    await expect(pane.getByText(CONV_SRC_REPLY).first()).toBeVisible();
  });

  // On disk before we detach it, or there is nothing whose survival to prove.
  await expect.poll(() => persistedAgentPaneIds(page), { timeout: 10_000 }).toHaveLength(1);

  await test.step("detach it: the pane closes, the session moves to Detached", async () => {
    await runCommand(page, "Detach this session");
    // The dedicated session pane is gone as a view…
    await expect(page.locator(".wb-pane-single")).toHaveCount(0);
    // …and off disk, so a reload has nothing to restore but the record.
    await expect.poll(() => persistedAgentPaneIds(page), { timeout: 10_000 }).toHaveLength(0);
    // …recorded under Detached, offered back by name.
    await expect(page.getByText("Detached", { exact: true })).toBeVisible();
    await expect(reattachButtons(page)).toHaveCount(1);
  });

  await test.step("the detach survives a reload — the record is on disk, not in memory", async () => {
    await page.reload();
    await workspaceReady(page);
    await expect(reattachButtons(page)).toHaveCount(1);
    // No session pane came back; the only way to it is Reattach.
    await expect(page.locator(".wb-pane-single")).toHaveCount(0);
  });

  await test.step("reattach: a pane opens back onto it and the transcript hydrates", async () => {
    await reattachButtons(page).click();
    const pane = page.locator(".wb-pane-single");
    // Hydrated from disk (PR1's path), reached through the reattach — the socket
    // only carries a session forward, so this history can only be the replay.
    await expect(pane.locator(".wb-msg-user", { hasText: CONV_SRC_TITLE })).toBeVisible();
    await expect(pane.getByText(CONV_SRC_REPLY)).toBeVisible();
    // Not a tombstone any more, and no longer in the Detached list.
    await expect(pane.locator(".wb-pane-note")).toHaveCount(0);
    await expect(reattachButtons(page)).toHaveCount(0);
  });
});

test("two detached sessions reattach independently through a reload", async ({ page }) => {
  await openApp(page);

  await test.step("two sessions, each in its own pane, each with its own message", async () => {
    await splitNewSession(page);
    await sendInActivePane(page, "alpha one");
    await expect(page.locator(".wb-panel-tab", { hasText: "alpha one" })).toBeVisible();

    await splitNewSession(page);
    await sendInActivePane(page, "bravo two");
    await expect(page.locator(".wb-panel-tab", { hasText: "bravo two" })).toBeVisible();

    await expect.poll(() => persistedAgentPaneIds(page), { timeout: 10_000 }).toHaveLength(2);
  });

  await test.step("detach both — two panes close, two records under Detached", async () => {
    await paneByTitle(page, "alpha one").locator(".wb-panel-tab").first().click();
    await runCommand(page, "Detach this session");
    await paneByTitle(page, "bravo two").locator(".wb-panel-tab").first().click();
    await runCommand(page, "Detach this session");
    await expect.poll(() => persistedAgentPaneIds(page), { timeout: 10_000 }).toHaveLength(0);
    await expect(reattachButtons(page)).toHaveCount(2);
  });

  await test.step("both survive a reload", async () => {
    await page.reload();
    await workspaceReady(page);
    await expect(reattachButtons(page)).toHaveCount(2);
  });

  await test.step("reattach both, and they are two independent conversations", async () => {
    // Reattaching one clears its mark, so the list shrinks by one each click.
    await reattachButtons(page).first().click();
    await expect(reattachButtons(page)).toHaveCount(1);
    await reattachButtons(page).first().click();
    await expect(reattachButtons(page)).toHaveCount(0);

    const alpha = paneByTitle(page, "alpha one");
    const bravo = paneByTitle(page, "bravo two");
    await expect(alpha).toHaveCount(1);
    await expect(bravo).toHaveCount(1);

    // A follow-up typed in one lands in that one and nowhere else — the whole
    // claim of plural panes through a save/restore round trip.
    await alpha.locator(".wb-panel-tab").first().click();
    await sendInActivePane(page, "alpha follow");
    await bravo.locator(".wb-panel-tab").first().click();
    await sendInActivePane(page, "bravo follow");

    await expect(alpha.locator(".wb-msg-user", { hasText: "alpha follow" })).toHaveCount(1);
    await expect(alpha.locator(".wb-msg-user", { hasText: "bravo follow" })).toHaveCount(0);
    await expect(bravo.locator(".wb-msg-user", { hasText: "bravo follow" })).toHaveCount(1);
    await expect(bravo.locator(".wb-msg-user", { hasText: "alpha follow" })).toHaveCount(0);
  });
});

test("detach the only pane in the dock — it stays and becomes its own Resume tombstone", async ({
  page,
}) => {
  // The one case the `dock.panels.length > 1` guard in `detachFocusedSession`
  // exists for: with the AgentBrowser closed and a single `agent#<id>` pane left
  // in the whole window, closing that pane on detach would land the user on an
  // empty dock with no route back — the Detached list lives only in the
  // AgentBrowser. So the pane must NOT close; it re-renders as its own Resume
  // tombstone in place. Every other detach test has >1 pane present, so this
  // branch had no coverage.
  await openApp(page);

  await test.step("one agent pane, then strip the dock down to just it", async () => {
    await splitNewSession(page);
    await sendInActivePane(page, "solo one");
    await expect(page.locator(".wb-panel-tab", { hasText: "solo one" })).toBeVisible();

    // Close every default panel. The floor refuses to close the last pane, so
    // this stops at exactly one — the `agent#<id>` — and no AgentBrowser.
    for (const name of ["Files", "Editor", "Terminal", "Agent"]) {
      await page.locator(".wb-panel-tab", { hasText: name }).first().click();
      await page.keyboard.press("Alt+X");
      await expect(page.locator(".wb-panel-tab", { hasText: name })).toHaveCount(0);
    }
    await expect(page.locator(".wb-panel-tab")).toHaveCount(1);
  });

  await test.step("detach it: the sole pane is not closed, it shows Resume in place", async () => {
    await page.locator(".wb-panel-tab", { hasText: "solo one" }).click();
    await runCommand(page, "Detach this session");

    // The guard held: the pane survived rather than emptying the dock…
    await expect(page.locator(".wb-pane-single")).toHaveCount(1);
    await expect(page.locator(".wb-panel-tab", { hasText: "solo one" })).toHaveCount(1);
    // …and re-rendered as its own Resume tombstone, the only route back — the
    // AgentBrowser and its Detached list are closed.
    await expect(page.getByText("This session is detached")).toBeVisible();
    await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
    await expect(reattachButtons(page)).toHaveCount(0);
  });
});

/**
 * Item 15 (PR1) — a live session's chat survives a reload.
 *
 * The gap PR #43 documented: `/ws/agent/{id}` carries a session only *forward*.
 * It replays pending permission and plan cards on connect but never the text and
 * tool rows already streamed — and a window holds its chat in memory — so a
 * reloaded tab reattaches to a still-running session over a blank chat. This is
 * that bug, driven the way a user hits it: resume a conversation into a live
 * pane, reload, and watch the history the socket would not replay come back from
 * disk (`GET /api/agents/{id}/transcript`, hydrated in `store.attachSession`).
 *
 * A resumed conversation is the honest vehicle: the fake agent spends no tokens
 * and writes no `~/.claude` transcript, so the messages that "streamed" have to
 * be ones that really exist on disk — which is exactly a resumed session's
 * transcript, read back through the same union-in-mtime-order path a live one
 * would use.
 */

import { expect, request, test, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp } from "./app";
import { CONV_SRC_REPLY, CONV_SRC_TITLE } from "./workspace";

const NO_LAYOUT = { current: null, current_name: null, saved: [] };

/** Run a QuickBar command by its row title. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/** The local id of the agent pane, once the debounced autosave has written it —
 * `agent#<local id>` is the whole of what a reload restores (`ui/src/panes.ts`),
 * so the pane only really survives if this string is on disk first. */
async function persistedAgentPaneId(page: Page): Promise<string | null> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).find((id) => id.startsWith("agent#")) ?? null;
}

test.beforeEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: NO_LAYOUT });
  await context.dispose();
});

test("a live session's chat comes back after a reload", async ({ page }) => {
  await openApp(page);

  await test.step("resume a conversation into a live pane, with its history", async () => {
    await runCommand(page, "Browse Claude conversations");
    await page.locator(".wb-conv-row", { hasText: CONV_SRC_TITLE }).first().click();
    // What was said before is on screen — the resume seeded the chat.
    await expect(page.locator(".wb-msg-user", { hasText: CONV_SRC_TITLE }).first()).toBeVisible();
    await expect(page.getByText(CONV_SRC_REPLY).first()).toBeVisible();
    // Live, not a tombstone: the pane found its session in the listing.
    await expect(page.locator(".wb-pane-note")).toHaveCount(0);
  });

  // The pane must be on disk before the reload, or there is nothing to restore
  // and the test would prove only that a fresh browser starts empty.
  await expect.poll(() => persistedAgentPaneId(page), { timeout: 10_000 }).not.toBeNull();

  // The dedicated session pane, scoped so the count below is about one pane and
  // not the default Agent panel that also mirrors the active conversation
  // (CLAUDE.md: an unscoped locator on a pane-internal class is a bug).
  const pane = page.locator(".wb-pane-single");

  await test.step("after a reload the chat is not blank — the history is replayed", async () => {
    await page.reload();
    // The in-memory chat was wiped by the reload; the pane reattaches to the
    // still-live session and refills from disk. Both the user turn and the
    // assistant reply are back.
    await expect(pane.locator(".wb-msg-user", { hasText: CONV_SRC_TITLE })).toBeVisible();
    await expect(pane.getByText(CONV_SRC_REPLY)).toBeVisible();
    await expect(page.locator(".wb-pane-note")).toHaveCount(0);
  });

  await test.step("and it is replayed once, not doubled", async () => {
    // A chat that re-fetched a history it already held would prepend it in front
    // of itself; the transcript carries exactly one user turn, so one row in the
    // pane is the whole proof the guard held.
    await expect(pane.locator(".wb-msg-user", { hasText: CONV_SRC_TITLE })).toHaveCount(1);
  });
});

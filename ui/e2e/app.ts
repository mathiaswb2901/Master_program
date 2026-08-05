/**
 * Helpers shared by the journeys. Deliberately thin: a journey should read like
 * what a user does, and every wait here is on an app signal (a rendered row, a
 * live PTY prompt, a settled frame) — never on a timer.
 */

import { expect, type Locator, type Page } from "@playwright/test";

/** Navigate, and wait for nothing. Split out of `openApp` for the one assertion
 * that races a timer: the shortcuts problems toast auto-dismisses ~6 s after
 * the load that raised it, which is inside `workspaceReady`'s own budget. */
export async function gotoApp(page: Page): Promise<void> {
  await page.goto("/");
}

/** Wait until the workspace tree has actually arrived. */
export async function workspaceReady(page: Page): Promise<void> {
  await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();
  // The tree renders empty until GET /api/files/tree resolves; the seeded
  // folder is the first thing every journey needs to exist.
  await expect(page.getByRole("treeitem", { name: "src" })).toBeVisible();
}

/** Open the app and wait until it is usable — how every journey starts. */
export async function openApp(page: Page): Promise<void> {
  await gotoApp(page);
  await workspaceReady(page);
}

export function treeItem(page: Page, name: string): Locator {
  return page.getByRole("treeitem", { name, exact: true });
}

/** Right-click a tree row and pick a context-menu entry. */
export async function treeMenu(page: Page, name: string, item: string): Promise<void> {
  await treeItem(page, name).click({ button: "right" });
  await page.getByRole("menuitem", { name: item }).click();
}

/** The Monaco editor for the active text tab. */
export function editor(page: Page): Locator {
  return page.locator(".wb-editor-body .monaco-editor").first();
}

/** Type into Monaco: focus it first, exactly as a user would. */
export async function typeInEditor(page: Page, text: string): Promise<void> {
  await editor(page).click();
  await page.keyboard.type(text);
}

/** The visible terminal instance (hidden tabs stay mounted, so be specific). */
export function terminal(page: Page): Locator {
  return page.locator(".wb-terminal:not(.is-hidden)");
}

/** Wait for a live PowerShell prompt, then type — no sleeps, no lost input. */
export async function typeInTerminal(page: Page, text: string): Promise<void> {
  await expect(terminal(page).locator(".xterm-rows")).toContainText("PS ", { timeout: 60_000 });
  await terminal(page).locator(".xterm-screen").click();
  await page.keyboard.type(text);
}

/** Type a command and press Enter — the one thing a shortcut may never do. */
export async function runInTerminal(page: Page, command: string): Promise<void> {
  await typeInTerminal(page, command);
  await page.keyboard.press("Enter");
}

/**
 * Terminal contents as one flat string.
 *
 * `textContent`, not `innerText`: xterm wraps one shell line across several row
 * elements, and innerText would put a newline inside a word the shell considers
 * unbroken \u2014 enough to hide a marker from a substring search. Non-breaking
 * spaces are xterm's cell padding.
 */
export async function terminalText(page: Page): Promise<string> {
  const text = (await terminal(page).locator(".xterm-rows").textContent()) ?? "";
  return text.replace(/\u00a0/g, " ");
}

/** Start a fresh live agent session (fake mode) and return its chat box. */
export async function newSession(page: Page): Promise<Locator> {
  await page.getByRole("button", { name: "New session" }).click();
  const input = page.locator(".wb-chat-input textarea");
  await expect(input).toBeVisible();
  return input;
}

/** Send one chat message and wait until it is on screen as the user's turn. */
export async function sendChat(page: Page, text: string): Promise<void> {
  const input = page.locator(".wb-chat-input textarea");
  await input.fill(text);
  await input.press("Enter");
  await expect(page.locator(".wb-msg-user").filter({ hasText: text })).toBeVisible();
}

/** The assistant blocks rendered so far (markdown, one per streamed turn). */
export function assistantBlocks(page: Page): Locator {
  return page.locator(".wb-msg-block");
}

/**
 * Is a settled tool row on screen *inside a turn that is still running*?
 *
 * Read in one DOM snapshot on purpose. `turn_done` settles every unsettled row
 * and flips the session to idle in the same store update, so a UI that had lost
 * per-tool settling can never satisfy this — while two separate `expect`s could
 * be satisfied by it, or could fail a correct UI, depending only on where the
 * turn ended between them. Poll it: the fake agent holds the turn open after
 * the tool result precisely so this becomes true for a while.
 */
export function toolSettledMidTurn(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const settled = document.querySelectorAll(".wb-tool-row.is-settled").length;
    const badge = document.querySelector(".wb-chat-header .wb-badge")?.textContent ?? "";
    return settled === 1 && badge.includes("Working");
  });
}

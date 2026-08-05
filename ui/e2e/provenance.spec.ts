/**
 * Journey 8 — provenance: reviewing what an agent changed.
 *
 * The fake agent writes a real file with a real `Write` tool call; the real
 * watcher notices; the correlator attributes it. Everything asserted here is
 * the user-visible end of that:
 *  - the tree marks the file, naming the session in its accessible name;
 *  - opening the file raises the bar that says who changed it, with what, when;
 *  - opening it is also the acknowledgment — the tree marker clears;
 *  - the bar's link opens that exact conversation in the Agent panel (the loop
 *    the chat's file links start, closed from the other side);
 *  - Dismiss puts the bar away.
 */

import { expect, test } from "@playwright/test";

import { newSession, openApp, sendChat } from "./app";
import { readWorkspaceFile } from "./workspace";

/** Must match `WRITE_TARGET_NAME` in services/fake_agent.py. */
const WRITTEN = "written-by-agent.md";
const SESSION_PROMPT = "write file please and note the provenance";
/** A second conversation, so the link has somewhere to come back from. */
const OTHER_PROMPT = "an unrelated second conversation";

test("an agent's file change is attributed, reviewable and acknowledged", async ({ page }) => {
  await openApp(page);
  await newSession(page);

  await test.step("the agent writes a file for real", async () => {
    await sendChat(page, SESSION_PROMPT);
    const row = page.locator(".wb-tool").filter({ hasText: "Write" });
    await expect(row.locator(".wb-tool-name")).toHaveText("Write");
    // Bytes on disk, not just a chat row: this is what the watcher reports.
    await expect.poll(() => readWorkspaceFile(WRITTEN)).toContain("Written by the fake agent");
  });

  const marked = page.getByRole("treeitem", { name: new RegExp(WRITTEN) });

  await test.step("the tree marks it, and names the session that did it", async () => {
    await expect(marked).toBeVisible();
    // The dot is never the only signal — the accessible name carries the claim.
    await expect(marked.getByRole("img")).toHaveAttribute(
      "aria-label",
      new RegExp(`Changed by ${SESSION_PROMPT}.*\\(Write\\)`),
    );
  });

  await test.step("opening it says who changed it, with what, and how long ago", async () => {
    await marked.click();
    const bar = page.locator(".wb-provenance-bar");
    await expect(bar).toBeVisible();
    await expect(bar).toContainText(SESSION_PROMPT);
    await expect(bar).toContainText("Write");
    await expect(bar).toContainText("just now");
  });

  await test.step("opening it is the acknowledgment: the marker clears", async () => {
    await expect(marked.getByRole("img")).toHaveCount(0);
    await expect(page.locator(".wb-tree-agent-dot")).toHaveCount(0);
  });

  await test.step("the bar links back to the exact session that changed it", async () => {
    // Move to a different conversation first, so "it opened" cannot be true by
    // accident — the panel has to actually switch back.
    await newSession(page);
    await sendChat(page, OTHER_PROMPT);
    const original = page.locator(".wb-msg-user").filter({ hasText: SESSION_PROMPT });
    const other = page.locator(".wb-msg-user").filter({ hasText: OTHER_PROMPT });
    await expect(original).toHaveCount(0); // we are somewhere else now

    await page.getByRole("button", { name: `Open session ${SESSION_PROMPT}` }).click();
    // Asserted on the conversation itself, not on the header label: the panel
    // is showing that exact history, not an empty shell with the right name.
    await expect(original).toBeVisible();
    await expect(other).toHaveCount(0);
  });

  await test.step("dismiss puts the bar away", async () => {
    await page.locator(".wb-provenance-bar").getByRole("button", { name: "Dismiss" }).click();
    await expect(page.locator(".wb-provenance-bar")).toHaveCount(0);
  });
});

/**
 * Journey 4 — chat against the fake agent.
 *
 * Asserts:
 *  - a new live session accepts a message and streams a reply that is rendered
 *    as markdown (bold + list elements, not raw asterisks);
 *  - "use tool" produces a tool row that names the real file it read, settles
 *    on its own result (not at turn end), and expands its excerpt on the
 *    chevron.
 */

import { expect, test } from "@playwright/test";

import { assistantBlocks, newSession, openApp, sendChat } from "./app";
import { NOTES_FILE, NOTES_MARKER } from "./workspace";

test("streamed markdown reply and an individually settling tool row", async ({ page }) => {
  await openApp(page);
  await newSession(page);

  await test.step("a message streams back as rendered markdown", async () => {
    await sendChat(page, "hello from the e2e suite");
    const reply = assistantBlocks(page).first();
    await expect(reply.locator("strong")).toHaveText("Fake agent");
    await expect(reply.locator("li").first()).toContainText("echo: hello from the e2e suite");
    // Rendered, not escaped: the source asterisks never reach the screen.
    await expect(reply).not.toContainText("**");
  });

  await test.step("a tool call gets its own row, settles, and expands", async () => {
    await sendChat(page, "use tool please");
    const row = page.locator(".wb-tool");
    await expect(row).toHaveCount(1);
    await expect(row.locator(".wb-tool-name")).toHaveText("Read");
    await expect(row.locator(".wb-tool-summary")).toContainText(NOTES_FILE);
    await expect(row.locator(".wb-tool-row.is-settled")).toBeVisible();
    await expect(row.locator(".wb-tool-row.is-failed")).toHaveCount(0);

    await expect(row.locator(".wb-tool-output")).toHaveCount(0);
    await row.getByRole("button", { name: "Show output" }).click();
    await expect(row.locator(".wb-tool-output")).toContainText(NOTES_MARKER);
  });
});

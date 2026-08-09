/**
 * Journey 4 — chat against the fake agent.
 *
 * Asserts:
 *  - a new live session accepts a message and streams a reply that is rendered
 *    as markdown (bold + list elements, not raw asterisks);
 *  - "use tool" produces a tool row that names the real file it read, settles
 *    on its own result *while the turn is still running* (not at turn end), and
 *    expands its excerpt on the chevron;
 *  - the ANVIL V5 hierarchy is what the browser actually computed: the tool
 *    row's status dot flips from the live amber to `--success` when the row
 *    settles, and a permission prompt drops its warn rim once it is answered
 *    (DESIGN.md §6.3, §2.4, §2.5, §7).
 *
 * The V5 assertions are scoped to the agent pane (`.wb-agent`) rather than the
 * page: several chat panes can be open at once, and an unscoped locator on a
 * pane-internal class is a singleton assumption wearing a selector.
 */

import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  assistantBlocks,
  newSession,
  openApp,
  sendChat,
  settledStyle,
  tokenColor,
  toolSettledMidTurn,
} from "./app";
import { NOTES_FILE, NOTES_MARKER } from "./workspace";

/** The Agent panel this journey works in — one pane, named as one. */
const agentPane = (page: Page): Locator => page.locator(".wb-agent");

/**
 * Wait until the turn is over.
 *
 * "use tool please and stay busy" holds the turn open on purpose, so a message
 * sent straight after it lands on a session that is still working — which is
 * how the permission step below failed the first time it ran: the card never
 * arrived, because the prompt that asks for it was never taken. The badge is
 * the app's own statement that it is ready.
 */
async function idle(pane: Locator): Promise<void> {
  await expect(pane.locator(".wb-chat-header .wb-badge")).toContainText("Idle");
}

test("streamed markdown reply and an individually settling tool row", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  const pane = agentPane(page);

  await test.step("a message streams back as rendered markdown", async () => {
    await sendChat(page, "hello from the e2e suite");
    const reply = assistantBlocks(page).first();
    await expect(reply.locator("strong")).toHaveText("Fake agent");
    await expect(reply.locator("li").first()).toContainText("echo: hello from the e2e suite");
    // Rendered, not escaped: the source asterisks never reach the screen.
    await expect(reply).not.toContainText("**");
  });

  await test.step("a tool call gets its own row, settles, and expands", async () => {
    // Both triggers: the fake agent holds the turn open *after* the tool's
    // result, so the row settling while the session is still working can only
    // come from the result frame — never from the turn-end fallback, which
    // settles rows and goes idle in the same update.
    await sendChat(page, "use tool please and stay busy");
    const row = pane.locator(".wb-tool");
    await expect(row).toHaveCount(1);
    await expect(row.locator(".wb-tool-name")).toHaveText("Read");
    await expect(row.locator(".wb-tool-summary")).toContainText(NOTES_FILE);
    await expect
      .poll(() => toolSettledMidTurn(page), { message: "the row settles on its own result" })
      .toBe(true);
    await expect(row.locator(".wb-tool-row.is-failed")).toHaveCount(0);

    await expect(row.locator(".wb-tool-output")).toHaveCount(0);
    await row.getByRole("button", { name: "Show output" }).click();
    await expect(row.locator(".wb-tool-output")).toContainText(NOTES_MARKER);
  });

  await test.step("the settled row says so with a dot as well as an edge (§6.3, §7)", async () => {
    // The 2px edge cannot carry an accessible name; the dot can, and §7 will
    // not take a colour as the only signal. Both are read from what the browser
    // computed, so a `var()` naming a token nobody defined fails here.
    //
    // Read through `settledStyle`, not once off `getComputedStyle`: the dot
    // cross-fades working → success over `--motion-tint-slow`, and a raw read
    // caught it a millisecond from the end (`rgb(4, 114, 44)` against the
    // token's `rgb(3, 114, 44)`). What is asserted is where it *lands*.
    const dot = pane.locator(".wb-tool-row .wb-dot");
    await expect(dot).toHaveAttribute("aria-label", "Succeeded");
    const success = await tokenColor(page, "--success");
    expect(await settledStyle(dot, "background-color")).toBe(success);
    expect(success).not.toBe(await tokenColor(page, "--agent-working"));
    // The pulse belongs to the state that is still changing, and this one is not.
    await expect(dot).not.toHaveClass(/u-agent-pulse/);
  });

  await test.step("a permission prompt stops being a warning once answered", async () => {
    await idle(pane);
    await sendChat(page, "ask permission before continuing");
    const card = pane.locator(".wb-perm-card");
    await expect(card).toContainText("echo scripted-permission");

    // While it is a question: the warn rim §6.3 specifies.
    expect(await settledStyle(card, "border-top-color")).toBe(await tokenColor(page, "--warn"));

    await card.getByRole("button", { name: "Allow" }).click();
    await expect(card.locator(".wb-perm-decision")).toHaveText("Allowed");

    // And once it is history it settles to the card language every other block
    // in the column uses. A rim that stays is a standing alarm about something
    // that already happened — §2.4's mistake, one hue over.
    await expect(card).toHaveClass(/is-decided/);
    expect(await settledStyle(card, "border-top-color")).toBe(
      await tokenColor(page, "--border-default"),
    );
    expect(
      await settledStyle(card.locator(".wb-perm-decision .wb-dot"), "background-color"),
    ).toBe(await tokenColor(page, "--success"));
  });

  await test.step("the picker says what a session is doing, in words (§6.12, §7)", async () => {
    // The fleet made legible: a row you can read without decoding a hue. Named
    // by *which* session it is — the selected one is the conversation this
    // journey has been talking to, and the suite shares a backend, so "the
    // first row" would be whichever earlier journey left one behind.
    const row = pane.locator(".wb-session-row.is-selected");
    await idle(pane);
    await sendChat(page, "stay busy for a moment");
    await expect(row.locator(".wb-session-state")).toHaveText("Working");
    const dot = row.locator(".wb-dot");
    // Settled for the same reason as the tool row's, and this one also carries
    // the app's one infinite animation — which `settledStyle` steps over.
    expect(await settledStyle(dot, "background-color")).toBe(
      await tokenColor(page, "--agent-working"),
    );
    await expect(dot).toHaveClass(/u-agent-pulse/);
    // …and it goes quiet on its own when the turn ends, back to the timestamp.
    await expect(row.locator(".wb-session-state")).toHaveCount(0);
    await expect(row.locator(".wb-session-time")).toBeVisible();
  });
});

/**
 * Journey 10 — your Claude plan limits, in the app.
 *
 * The order of these steps is the feature: the **degraded** state comes first,
 * because it is what a real account is most likely to show. The figures only
 * exist once a live session's stream has carried a rate-limit transition, and
 * an account may never emit one at all — so "nothing has arrived" is not an
 * error path here, it is the default one, and it has to explain itself.
 *
 * Then the fake agent emits the transitions the CLI really sends (one event per
 * window, `services/fake_agent.py`), and the same surfaces are asserted with
 * figures in them: a meter per reported window with its reset time and its own
 * age, a bar near its cap carrying *words* as well as a colour (DESIGN.md §7),
 * and the status-bar reading that was silent a moment ago.
 *
 * Last, a reload: the live path (`/ws/events`) and the load path
 * (`GET /api/usage`) must agree, because a window opened after the transition
 * is the common case in a multi-window workbench.
 *
 * This journey must run before nothing and after nothing — it is the only one
 * that sends `usage please`, so it owns the shared backend's usage state.
 */

import { expect, test, type Page } from "@playwright/test";

import { newSession, openApp, sendChat } from "./app";

/** Open the Usage panel the way a user reaches a tool that is not on screen:
 * the QuickBar, through the registry. */
async function openUsagePanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">plan usage");
  await quickbar.locator(".wb-qb-row", { hasText: "Show Claude plan usage" }).first().click();
  await expect(page.locator(".wb-usage")).toBeVisible();
}

test("plan usage: the honest empty state, then real meters", async ({ page }) => {
  await openApp(page);

  await test.step("with nothing reported, the status bar says nothing", async () => {
    // §6.7: a quiet bar means nothing needs you. The capability is still
    // reachable — the next step reaches it.
    await expect(page.locator(".wb-usage-status")).toHaveCount(0);
  });

  await test.step("the panel explains why it is empty instead of showing zeroes", async () => {
    await openUsagePanel(page);
    const panel = page.locator(".wb-usage");
    await expect(panel).toContainText("No plan usage reported yet");
    await expect(panel).toContainText("Never updated");
    // The two facts a user needs: it arrives on a transition, and nothing here
    // can ask for it.
    await expect(panel).toContainText("only when one of them changes");
    await expect(panel).toContainText("no way to ask for them");
    // Not one meter — and above all not a 0% one.
    await expect(page.locator(".wb-usage-track")).toHaveCount(0);
    // What we *do* know, under its own heading.
    await expect(panel).toContainText("Session cost — not plan usage");
  });

  await test.step("a live session's stream carries the limits", async () => {
    await newSession(page);
    await sendChat(page, "usage please");
    // Three windows, in presentation order, each a meter.
    await expect(page.locator(".wb-usage-meter")).toHaveCount(3);
  });

  await test.step("each meter carries its figure, its reset and its own age", async () => {
    const meters = page.locator(".wb-usage-meter");
    await expect(meters.nth(0)).toContainText("5-hour limit");
    await expect(meters.nth(0)).toContainText("42%");
    await expect(meters.nth(0)).toContainText("Resets in");
    await expect(meters.nth(0)).toContainText("old");
    await expect(meters.nth(1)).toContainText("Weekly, all models");
    await expect(meters.nth(2)).toContainText("Weekly, Opus");
    // Nothing invented for the window the SDK never mentioned.
    await expect(page.locator(".wb-usage")).not.toContainText("Weekly, Sonnet");
    // The stamp caveat 1 demands, on the panel as a whole.
    await expect(page.locator(".wb-usage-stamp")).toContainText("Updated");
  });

  await test.step("a bar near its cap says so in words, not only in colour", async () => {
    // DESIGN.md §7 — colour is never the only signal.
    await expect(page.locator(".wb-usage-meter.is-warning")).toContainText("Approaching limit");
    await expect(page.locator(".wb-usage-meter.is-critical")).toContainText("Limit reached");
    // Overage state rides another window's event and is reported as text.
    await expect(page.locator(".wb-usage-overage")).toContainText("unavailable");
  });

  await test.step("the status bar now carries the loudest window", async () => {
    const reading = page.locator(".wb-usage-status");
    await expect(reading).toHaveCount(1);
    await expect(reading).toHaveClass(/is-critical/);
    await expect(reading).toContainText("7d Opus");
    await expect(reading).toContainText("100%");
    await expect(reading).toContainText("Limit reached");
  });

  await test.step("a window opened afterwards loads the same figures", async () => {
    // The reconnect/initial-load path: GET /api/usage, not the socket frame
    // that has already been and gone.
    await page.reload();
    await expect(page.locator(".wb-usage-status")).toContainText("100%");
    await openUsagePanel(page);
    await expect(page.locator(".wb-usage-meter")).toHaveCount(3);
    await expect(page.locator(".wb-usage")).toContainText("42%");
  });

  await test.step("the meters still read at a glance when the panel fills the window", async () => {
    // Alt+M is focus mode (§6.9). The grid reflows; every meter stays on screen.
    await page.locator(".wb-usage").click();
    await page.keyboard.press("Alt+m");
    const meters = page.locator(".wb-usage-meter");
    await expect(meters).toHaveCount(3);
    for (let index = 0; index < 3; index += 1) {
      await expect(meters.nth(index)).toBeVisible();
    }
    await page.keyboard.press("Alt+m");
  });
});

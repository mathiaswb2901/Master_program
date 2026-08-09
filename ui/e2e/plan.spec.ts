/**
 * Journey 5 — visual plan artifacts, end to end.
 *
 * Asserts:
 *  - `present_plan` renders a native card with the agent's recommended option
 *    pre-selected and carrying the accent treatment (DESIGN.md principle 3:
 *    accent lands on the recommendation and on Approve, nowhere else);
 *  - the user can switch to the other option and approve;
 *  - the card then settles read-only (verdict shown, controls disabled);
 *  - the *agent* received that decision — the fake echoes the chosen option
 *    back as text, which is the only proof that the typed PlanResponse made it
 *    across the bridge.
 */

import { expect, test } from "@playwright/test";

import { assistantBlocks, newSession, openApp, sendChat, settledStyle, tokenColor } from "./app";

test("plan card: recommendation pre-selected, decision round-trips", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  await sendChat(page, "plan please");

  const card = page.locator(".wb-plan-card");
  const recommended = card.locator(".wb-plan-option.is-recommended");
  const other = card.locator(".wb-plan-option").filter({ hasText: "UTC everywhere" });

  await test.step("the recommended option is pre-selected and accented", async () => {
    await expect(card).toBeVisible();
    await expect(card.locator(".wb-plan-title")).toHaveText("Scripted plan");
    await expect(recommended).toHaveClass(/is-selected/);
    await expect(recommended.locator("input[type=radio]")).toBeChecked();
    await expect(recommended.locator(".wb-plan-rec")).toHaveText("Recommended");
    // Accent is a visible difference, not just a class name.
    const selectedBorder = await recommended.evaluate((el) => getComputedStyle(el).borderColor);
    const plainBorder = await other.evaluate((el) => getComputedStyle(el).borderColor);
    expect(selectedBorder).not.toBe(plainBorder);
    // The step list rendered its file ref as a real, openable chip.
    await expect(card.locator(".wb-plan-file")).toHaveText("notes.md");
  });

  await test.step("*Recommended* is an outlined neutral, not a second amber", async () => {
    // §2.4 demoted it when ANVIL landed: a recommendation is a standing
    // property of an option — true before you opened the card and still true
    // after — so it cannot wear the colour that means *now*. Asserted against
    // what the browser computed, because the point is that it is not amber on
    // screen, whatever the class is called.
    const pill = recommended.locator(".wb-plan-rec");
    const color = await settledStyle(pill, "color");
    const border = await settledStyle(pill, "border-top-color");
    const accent = await tokenColor(page, "--accent");
    expect(color).not.toBe(accent);
    expect(border).not.toBe(accent);
    expect(border).toBe(await tokenColor(page, "--border-strong"));
  });

  await test.step("a trade-off is legible at a glance, sign and colour together (§7)", async () => {
    const pro = card.locator(".wb-plan-sign.is-pro").first();
    const con = card.locator(".wb-plan-sign.is-con").first();
    await expect(pro).toHaveText("+");
    await expect(con).toHaveText("−");
    expect(await settledStyle(pro, "color")).toBe(await tokenColor(page, "--success"));
    expect(await settledStyle(con, "color")).toBe(await tokenColor(page, "--error"));
  });

  await test.step("choosing the other option and approving settles the card", async () => {
    await other.click();
    await expect(other.locator("input[type=radio]")).toBeChecked();
    await card.getByRole("button", { name: "Approve" }).click();

    await expect(card.locator(".wb-plan-verdict")).toHaveText("Approved");
    await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(other.locator("input[type=radio]")).toBeDisabled();
    await expect(other).toContainText("Chosen");

    // The verdict says itself twice — the word and a semantic dot — because
    // colour is never the only signal (§7), and the word carries the reading
    // because an 11px label in a semantic colour misses the contrast floor.
    await expect(card).toHaveClass(/is-settled/);
    expect(
      await settledStyle(card.locator(".wb-plan-verdict .wb-dot"), "background-color"),
    ).toBe(await tokenColor(page, "--success"));
  });

  await test.step("the agent received the decision", async () => {
    await expect(assistantBlocks(page).last()).toContainText("plan approve: approach=utc");
  });
});

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

import { assistantBlocks, newSession, openApp, sendChat } from "./app";

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

  await test.step("choosing the other option and approving settles the card", async () => {
    await other.click();
    await expect(other.locator("input[type=radio]")).toBeChecked();
    await card.getByRole("button", { name: "Approve" }).click();

    await expect(card.locator(".wb-plan-verdict")).toHaveText("Approved");
    await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(other.locator("input[type=radio]")).toBeDisabled();
    await expect(other).toContainText("Chosen");
  });

  await test.step("the agent received the decision", async () => {
    await expect(assistantBlocks(page).last()).toContainText("plan approve: approach=utc");
  });
});

/**
 * Journey 10 — pointing at part of an artifact and saying what is wrong with it.
 *
 * The moment this PR exists for: the user reads a drawn card, points at a
 * *cell* and at a *point on a chart*, says what is wrong with each, and
 * decides — without ever going back to the chat box.
 *
 * Driven **from the keyboard first**, because that is the claim DESIGN.md §7
 * makes and the one no unit test can check: the mode is entered with a chord,
 * every part is a real button with an accessible name, and Escape unwinds the
 * open note before it unwinds the mode.
 *
 * Asserts, in the order they would break:
 *  - `Alt+A` enters annotate mode on the pending card;
 *  - a table cell carries a **semantic path** (`leaf 3, row 1, col "Price"`),
 *    not a selector, and picking it opens a note editor already focused;
 *  - a chart point is reachable in two keyboard steps and is labelled in the
 *    market's own clock — the 25-hour day's 12th grid point is 11:00;
 *  - a note can be edited and another removed, without touching the rest;
 *  - approving sends **one** `PlanResponse` carrying the verdict *and* the
 *    notes: the fake agent echoes each anchor as a slash path, which is the
 *    only proof the exact part reached the agent.
 */

import { expect, test } from "@playwright/test";

import { assistantBlocks, newSession, openApp, sendChat } from "./app";

test("annotate mode: point at a cell and a chart point, then decide", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  await sendChat(page, "visual please");

  const card = page.locator(".wb-plan-card");
  const visual = card.locator(".wb-vis");
  const toggle = card.getByRole("button", { name: "Annotate", exact: true });
  const rows = card.locator(".wb-plan-note-row");
  const editing = card.locator(".wb-plan-note-row.is-editing textarea");
  await expect(visual).toBeVisible();

  await test.step("the chord enters annotate mode", async () => {
    // The chord, not the button: a mode reachable only by mouse fails §7.
    await card.locator(".wb-plan-title").click();
    await page.keyboard.press("Alt+a");
    await expect(card).toHaveClass(/is-annotating/);
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
  });

  await test.step("a table cell becomes a part you can point at", async () => {
    // Found the way a user or a screen reader finds it — by what it says — and
    // only then checked for the path it carries. "After" is the fourth leaf of
    // the scene (metrics, chart, then the two split tables) and the second
    // 02:00 is its row 1.
    const cell = visual.getByRole("button", { name: "Annotate After, row 2, Price" });
    await expect(cell).toHaveCount(1);
    // The anchor: a semantic path into the payload. Not a selector, not a
    // position on screen — and this exact addressing is what reaches the agent.
    await expect(cell).toHaveAttribute(
      "data-anchor",
      '["part","scene","leaf",3,"row",1,"col","Price"]',
    );
    await cell.click();

    // Picking a part opens its note with the caret already in it: a mode that
    // sends you back to the mouse is half a mode.
    await expect(editing).toBeFocused();
    await page.keyboard.type("This is the second 02:00, not the first.");
    await expect(rows.first()).toContainText("This is the second 02:00, not the first.");
    await expect(rows.first()).toContainText("After, row 2, Price");
  });

  await test.step("Escape closes the note without leaving the mode", async () => {
    await page.keyboard.press("Escape");
    await expect(editing).toHaveCount(0);
    await expect(card).toHaveClass(/is-annotating/);
  });

  await test.step("a chart point is reachable in two keyboard steps", async () => {
    // A chart may legally hold 2,400 points, so they are not all tab stops:
    // choose the series, then the point. Both are ordinary buttons.
    const picker = visual.locator(".wb-vis-points-pick");
    await expect(picker).toBeVisible();
    await picker.getByRole("button", { name: "SE3 day-ahead" }).click();

    const strip = visual.locator(".wb-vis-points-list");
    const point = strip.locator(".wb-vis-part").nth(12);
    // 2026-10-25 in Europe/Stockholm has two 02:00s, so the 12th grid point is
    // 11:00 — the strip is labelled in market time, like the axis above it.
    await expect(point).toHaveAttribute(
      "aria-label",
      "Annotate Price and dispatch, SE3 day-ahead, 11:00, -3.8",
    );
    await expect(point).toHaveAttribute(
      "data-anchor",
      '["part","scene","leaf",1,"series","SE3 day-ahead","point",12]',
    );
    await point.click();
    await expect(editing).toBeFocused();
    await page.keyboard.type("Negative here is real, keep it.");
    await page.keyboard.press("Escape");
  });

  await test.step("a phrase of prose is a target too", async () => {
    // The fourth anchor kind. The offsets are into the **source** string: the
    // second sentence starts at character 48 and runs to 87, the caveat's
    // length. An offset taken from the rendered DOM would name other
    // characters entirely.
    const phrase = card.getByRole("button", {
      name: "Annotate “The second 02:00 uses the fold-1 curve.”",
    });
    await expect(phrase).toHaveCount(1);
    await phrase.click();
    await expect(editing).toBeFocused();
    await page.keyboard.type("Say which curve, by name.");
    await page.keyboard.press("Escape");
  });

  await test.step("a note can be edited, and another removed", async () => {
    await expect(rows).toHaveCount(3);
    await rows.first().getByRole("button", { name: "Edit", exact: true }).click();
    await expect(editing).toBeFocused();
    await page.keyboard.type(" Check the fold flag.");
    await expect(rows.first()).toContainText("Check the fold flag.");
    await page.keyboard.press("Escape");

    // A fourth note, so removing one proves removal is targeted rather than a
    // reset of the whole draft.
    await visual.locator(".wb-vis-metrics .wb-vis-part").first().click();
    await page.keyboard.type("Delivery hours look right.");
    await page.keyboard.press("Escape");
    await expect(rows).toHaveCount(4);

    const doomed = rows.filter({ hasText: "Delivery hours look right." });
    await doomed.getByRole("button", { name: /^Remove/ }).click();
    await expect(rows).toHaveCount(3);
    await expect(card).not.toContainText("Delivery hours look right.");
  });

  await test.step("a whole row is a target, with no column named", async () => {
    // The row handle: the anchor for "this row is wrong", not one cell of it.
    // It is also the anchor kind that used to disappear the moment the mode
    // went off — asserted below, after the decision settles the card.
    const handle = visual.getByRole("button", { name: "Annotate After, row 2", exact: true });
    await expect(handle).toHaveAttribute("data-anchor", '["part","scene","leaf",3,"row",1]');
    await handle.click();
    await expect(editing).toBeFocused();
    await page.keyboard.type("This whole row is the fold-1 hour.");
    await page.keyboard.press("Escape");
    await expect(rows).toHaveCount(4);
  });

  await test.step("the notes travel with the verdict, in one response", async () => {
    await card.getByRole("button", { name: "Approve" }).click();
    await expect(card.locator(".wb-plan-verdict")).toHaveText("Approved");

    // The fake agent echoes each note as `<node>/<path…>=<text>`. This is the
    // assertion the whole design exists for: the agent was told *which part*,
    // as a path into its own payload.
    const echo = assistantBlocks(page).last();
    await expect(echo).toContainText("plan approve: no choices");
    await expect(echo).toContainText(
      "scene/leaf/3/row/1/col/Price=This is the second 02:00, not the first. Check the fold flag.",
    );
    await expect(echo).toContainText(
      "scene/leaf/1/series/SE3 day-ahead/point/12=Negative here is real, keep it.",
    );
    // A row with no column named: the anchor stops at the row.
    await expect(echo).toContainText("scene/leaf/3/row/1=This whole row is the fold-1 hour.");
    // The range: offsets into the caveat's *source* string, not the page.
    await expect(echo).toContainText("caveat/from/48/to/87=Say which curve, by name.");
    // …and the removed one did not travel.
    await expect(echo).not.toContainText("Delivery hours look right.");
  });

  await test.step("the settled card is read-only, mode and all", async () => {
    await expect(card).not.toHaveClass(/is-annotating/);
    await expect(toggle).toHaveCount(0);
  });

  await test.step("every note keeps its marker on the settled card", async () => {
    // A note you can only see by turning a mode back on is a note you will not
    // act on — and the mode cannot come back once the card is decided. So every
    // anchor kind keeps its ✎ in place, inert. The two that used to vanish with
    // the mode are a *row* (whose handle column was drawn only for pickers) and
    // a *chart point* (whose whole strip was), so both are asserted here.
    const noted = (anchor: string) => visual.locator(`[data-anchor='${anchor}']`);
    for (const anchor of [
      '["part","scene","leaf",3,"row",1]',
      '["part","scene","leaf",1,"series","SE3 day-ahead","point",12]',
      '["part","scene","leaf",3,"row",1,"col","Price"]',
    ]) {
      await expect(noted(anchor)).toHaveCount(1);
      await expect(noted(anchor)).toHaveClass(/is-noted/);
      await expect(noted(anchor).locator(".wb-vis-pin")).toBeVisible();
    }
    // Inert, not merely restyled: nothing left to click or tab into.
    await expect(visual.locator("button")).toHaveCount(0);
    // …and the picker itself is gone — the strip that survives is the notes.
    await expect(visual.locator(".wb-vis-points-pick")).toHaveCount(0);
  });
});

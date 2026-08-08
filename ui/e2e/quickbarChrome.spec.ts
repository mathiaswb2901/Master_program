/**
 * ANVIL V3 — what the QuickBar actually looks like, in a real browser.
 *
 * `quickbar.spec.ts` drives the palette's *behaviour* and V3 left it untouched
 * on purpose — it passing unchanged is the proof that dressing this surface
 * changed nothing about how it works. `quickbarChrome.test.ts` asserts what the
 * stylesheet declares. This is the third leg: the computed result, on the built
 * bundle, of those declarations meeting the markup — the amber wash lands on the
 * row Enter would run and on no other, the match highlight is under the letters
 * the user typed, and arrowing through a sectioned list never parks the selection
 * behind a pinned header.
 *
 * Structure and computed *tokens*, never a screenshot and never a hex literal: a
 * pixel baseline on the surface every other journey opens would fail on a
 * font-hinting difference and say nothing about the design, and a hard-coded
 * `rgb(251, 191, 36)` would only be true in one of the two themes (the E2E
 * browser's `prefers-color-scheme` decides which one the app boots into). Every
 * colour below is resolved from the page's own tokens through a probe element,
 * so this reads the same contract `tokens.css` publishes.
 */

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { openApp } from "./app";

/** A design token as the browser resolves it, in this page's live theme. */
async function tokenColor(page: Page, name: string): Promise<string> {
  return page.evaluate((token) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${token})`;
    document.body.append(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  }, name);
}

test("the palette's chrome: one amber, a legible match, and a selection that stays clear", async ({
  page,
}) => {
  await openApp(page);
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  const accent = await tokenColor(page, "--accent");
  const wash = await tokenColor(page, "--surface-selected");
  const textPrimary = await tokenColor(page, "--text-primary");
  const textSecondary = await tokenColor(page, "--text-secondary");

  await test.step("an unsearched list carries no highlight at all", async () => {
    // The step-back only applies when there is something matched to step back
    // *from*; a palette that dimmed every label the moment it opened would be
    // dressing an empty query as a failed search.
    await page.keyboard.press("Control+Shift+P");
    await expect(quickbar).toBeVisible();
    await expect(quickbar.locator(".wb-qb-hit")).toHaveCount(0);
    await expect(quickbar.locator(".is-matched")).toHaveCount(0);
  });

  await test.step("the amber marks the row Enter would run, and only that row", async () => {
    await page.keyboard.type("termi");
    const rows = quickbar.locator(".wb-qb-row");
    await expect(rows.first()).toHaveClass(/is-selected/);
    await expect(rows.first()).toHaveCSS("background-color", wash);
    await expect(rows.first()).toHaveCSS("border-left-color", accent);
    await expect(rows.first()).toHaveCSS("border-left-width", "2px");

    // Exactly one element in the whole overlay wears it: the selected row is
    // *where I am*, and a second amber in a 640px surface is how the first stops
    // meaning anything (§2.4). The caret is the sanctioned other spend and is
    // not a painted box, so it is not — and cannot be — counted here.
    const ambers = await quickbar.evaluate(
      (dialog, colours) =>
        [...dialog.querySelectorAll("*")].filter((element) => {
          const style = getComputedStyle(element);
          return style.backgroundColor === colours.wash || style.borderLeftColor === colours.accent;
        }).length,
      { wash, accent },
    );
    expect(ambers, "one amber in the overlay — the selected row").toBe(1);

    // …and an unselected row keeps the same box, so the selection moving never
    // shifts a label sideways.
    const second = rows.nth(1);
    await expect(second).toHaveCSS("border-left-color", "rgba(0, 0, 0, 0)");
    await expect(second).toHaveCSS("border-left-width", "2px");
  });

  await test.step("the highlight sits under the letters that were typed", async () => {
    const title = quickbar.locator(".wb-qb-row").first().locator(".wb-qb-row-title");
    await expect(title).toHaveClass(/is-matched/);
    const hits = await title.locator(".wb-qb-hit").allInnerTexts();
    expect(hits.join("").toLowerCase(), "the matched run, and nothing else").toBe("termi");
    // A hierarchy, not a colour: the matched run is the primary text and the
    // label around it steps back one notch. No amber is spent on it — it would
    // be on every visible row at once, which is the definition of decoration.
    await expect(title.locator(".wb-qb-hit").first()).toHaveCSS("color", textPrimary);
    await expect(title).toHaveCSS("color", textSecondary);
  });

  await test.step("the caret is the field you are typing in (§2.4)", async () => {
    await expect(quickbar.locator(".wb-qb-input")).toHaveCSS("caret-color", accent);
  });

  await test.step("arrowing through a sectioned list never parks behind a header", async () => {
    // Back to the full command list, which carries four section headers, and the
    // headers are sticky so they cover what scrolls under them. `block:
    // "nearest"` stops the moment a row's box is inside the viewport — which,
    // arrowing *upwards* into a section, is the moment it is behind that
    // section's pinned header. `scroll-margin-top` is what keeps it clear.
    await quickbar.locator(".wb-qb-input").fill(">");
    await expect(quickbar.locator(".wb-qb-cat").first()).toBeVisible();
    const rows = await quickbar.locator(".wb-qb-row").count();
    const geometry = (): Promise<{ clipped: boolean; covered: boolean; headers: number } | null> =>
      page.evaluate(() => {
        const row = document.querySelector<HTMLElement>(".wb-qb-row.is-selected");
        const results = document.querySelector<HTMLElement>(".wb-qb-results");
        if (row === null || results === null) return null;
        const box = row.getBoundingClientRect();
        const port = results.getBoundingClientRect();
        const headers = [...document.querySelectorAll<HTMLElement>(".wb-qb-cat")].map((header) =>
          header.getBoundingClientRect(),
        );
        return {
          clipped: box.top < port.top - 0.5 || box.bottom > port.bottom + 0.5,
          covered: headers.some(
            (header) => header.bottom > box.top + 0.5 && header.top < box.bottom - 0.5,
          ),
          headers: headers.length,
        };
      });

    // All the way down, then all the way back up one row at a time — the whole
    // list, because which row lands against the pinned header depends on how
    // many commands are registered, and a fixed number of presses is a test that
    // stops proving anything the day a tool adds one.
    for (let i = 0; i < rows; i++) await page.keyboard.press("ArrowDown");
    expect((await geometry())?.headers, "the list really is sectioned").toBeGreaterThan(1);
    for (let i = 0; i < rows; i++) {
      await page.keyboard.press("ArrowUp");
      const step = await geometry();
      expect(step, "a row is always selected").not.toBeNull();
      expect(step?.clipped, `row from the bottom #${i}: inside the list's viewport`).toBe(false);
      expect(step?.covered, `row from the bottom #${i}: no pinned header over it`).toBe(false);
    }
  });

  await page.keyboard.press("Escape");
  await expect(quickbar).toBeHidden();

  await test.step("a file query that spans a folder and a name is highlighted in both", async () => {
    // The case that made the first cut of this useless, found by driving the
    // running app rather than by reading the diff: a file row is *ranked* on its
    // whole path and *shown* as a name beside its folder, so re-matching the
    // query against either label alone finds nothing — `target/se3` is a real
    // match for `analysis/target/se3-targets-2026.csv` and matches neither half.
    // The row now splits the match it already has across its two labels.
    await page.keyboard.press("Control+P");
    await expect(quickbar).toBeVisible();
    await page.keyboard.type("target/se3");
    const row = quickbar.locator(".wb-qb-row").first();
    await expect(row).toContainText("se3-targets-2026.csv");

    const detail = await row.locator(".wb-qb-row-detail .wb-qb-hit").allInnerTexts();
    const title = await row.locator(".wb-qb-row-title .wb-qb-hit").allInnerTexts();
    expect(detail.join(""), "the folder carries the part of the query that is a folder").toBe(
      "target",
    );
    expect(title.join(""), "and the name carries the rest").toBe("se3");

    await page.keyboard.press("Escape");
    await expect(quickbar).toBeHidden();
  });
});

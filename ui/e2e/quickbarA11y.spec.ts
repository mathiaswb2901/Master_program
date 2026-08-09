/**
 * The QuickBar as a *real* modal, and as a real combobox.
 *
 * Two findings from the accessibility audit, reproduced here the way a user
 * hits them and then pinned as regressions. Both are about the app's own
 * universal escape hatch, which is the worst possible surface to be locked out
 * of: every command in the window is run through it.
 *
 *  1. **It was a `role="dialog"` and nothing else.** No `aria-modal`, no focus
 *     trap, and the only Escape handler was an `onKeyDown` on the input — so a
 *     single `Shift+Tab` put focus on a control *behind the scrim*, invisible
 *     under 60% black and unreachable by mouse, and from there Escape did
 *     nothing at all. The way out was to know the backdrop is clickable.
 *  2. **The selected row was invisible to assistive tech.** The amber wash and
 *     the 2px edge say "this is what Enter runs" to a pair of eyes and to
 *     nothing else: no `role="combobox"`, no `aria-activedescendant`, no
 *     listbox, no options, no announcement of how many results a query found.
 *
 * `quickbar.spec.ts` (behaviour) and `quickbarChrome.spec.ts` (the look) are
 * the other two legs and both stay untouched — this fix is semantics and focus
 * only, and those two passing unchanged is what says so.
 */

import { expect, test } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";

import { openApp, treeItem } from "./app";

const palette = (page: Page): Locator => page.getByRole("dialog", { name: "Quick open" });

/** Where the keyboard actually is, as a selector path the assertion can print. */
async function focusReport(page: Page): Promise<{ insidePalette: boolean; element: string }> {
  return page.evaluate(() => {
    const active = document.activeElement;
    const describe = (node: Element | null): string => {
      if (node === null) return "<none>";
      const classes = node.className.toString().trim().split(/\s+/).filter(Boolean).join(".");
      return `${node.tagName.toLowerCase()}${classes === "" ? "" : `.${classes}`}`;
    };
    return {
      insidePalette: active !== null && active.closest(".wb-qb") !== null,
      element: describe(active),
    };
  });
}

test("the palette traps focus, answers Escape anywhere, and speaks its results", async ({
  page,
}) => {
  await openApp(page);

  await test.step("Shift+Tab does not walk out behind the scrim", async () => {
    // The repro. On master the first Shift+Tab leaves the palette entirely —
    // focus lands on the last focusable control of the status bar, which is
    // under the backdrop and cannot be seen or clicked. Pressed at the page
    // level, exactly as a keyboard user does it.
    await page.keyboard.press("Control+Shift+P");
    await expect(palette(page)).toBeVisible();
    await expect(palette(page).locator(".wb-qb-input")).toBeFocused();

    await page.keyboard.press("Shift+Tab");
    const back = await focusReport(page);
    expect(back.insidePalette, `Shift+Tab landed on ${back.element}`).toBe(true);

    // Forwards too, and more than once: a trap that only holds for one press is
    // a trap with an exit at the far end of the tab ring.
    for (let step = 0; step < 3; step += 1) {
      await page.keyboard.press("Tab");
      const forward = await focusReport(page);
      expect(forward.insidePalette, `Tab #${step} landed on ${forward.element}`).toBe(true);
    }
  });

  await test.step("…and Escape still closes it from wherever that left the keyboard", async () => {
    // The second half of the same bug: the only Escape handler was on the
    // input, so focus leaving the input took Escape with it. The handler is now
    // a window-level capture listener (the pattern `Modal.tsx` already ships,
    // where it also wins over Monaco and xterm).
    await page.keyboard.press("Escape");
    await expect(palette(page)).toBeHidden();
  });

  await test.step("it announces itself as a modal dialog", async () => {
    await page.keyboard.press("Control+Shift+P");
    await expect(palette(page)).toHaveAttribute("aria-modal", "true");
  });

  await test.step("the input is a combobox that names the row Enter would run", async () => {
    const input = palette(page).locator(".wb-qb-input");
    await expect(input).toHaveAttribute("role", "combobox");
    await expect(input).toHaveAttribute("aria-expanded", "true");
    const listboxId = await input.getAttribute("aria-controls");
    expect(listboxId, "the combobox must name its popup").toBeTruthy();
    await expect(palette(page).locator(`#${String(listboxId)}`)).toHaveAttribute("role", "listbox");

    // The load-bearing assertion: the option `aria-activedescendant` points at
    // is the same row the amber marks. A screen reader reads that row on every
    // arrow press; before this fix it read nothing, because focus never moved.
    const activeId = async (): Promise<string> => String(await input.getAttribute("aria-activedescendant"));
    const selected = palette(page).locator(".wb-qb-row.is-selected");
    await expect(selected).toHaveCount(1);
    await expect(selected).toHaveAttribute("id", await activeId());
    await expect(selected).toHaveAttribute("aria-selected", "true");
    await expect(selected).toHaveAttribute("role", "option");

    // …and it follows the arrow key rather than being set once at open.
    const first = await activeId();
    await page.keyboard.press("ArrowDown");
    const second = await activeId();
    expect(second).not.toBe(first);
    await expect(palette(page).locator(".wb-qb-row.is-selected")).toHaveAttribute("id", second);
    // Exactly one option claims selection, whatever the list is doing.
    await expect(palette(page).locator('.wb-qb-row[aria-selected="true"]')).toHaveCount(1);
  });

  await test.step("options are not tab stops — the combobox owns the keyboard", async () => {
    // Why the trap above is trivial rather than a ring of fifty rows: per the
    // ARIA combobox pattern the options are reached with `aria-activedescendant`
    // and are never in the tab sequence. Fifty file rows in the tab order was
    // its own bug.
    const tabbable = await palette(page)
      .locator('.wb-qb-row:not([tabindex="-1"])')
      .count();
    expect(tabbable, "no result row is a tab stop").toBe(0);
  });

  await test.step("a query that finds nothing collapses the popup and says so", async () => {
    const input = palette(page).locator(".wb-qb-input");
    await input.fill(">zzzzzzz-no-such-command");
    await expect(input).toHaveAttribute("aria-expanded", "false");
    await expect(palette(page).locator(".wb-qb-row")).toHaveCount(0);
    // Polite, debounced, and screen-reader-only — the visible empty state is
    // unchanged, this is the half a blind user gets.
    await expect(palette(page).locator("[aria-live='polite']")).toHaveText("No results");
  });

  await test.step("and a query that finds something announces the count", async () => {
    await palette(page).locator(".wb-qb-input").fill(">terminal");
    const rows = await palette(page).locator(".wb-qb-row").count();
    expect(rows).toBeGreaterThan(0);
    await expect(palette(page).locator("[aria-live='polite']")).toHaveText(
      `${rows} result${rows === 1 ? "" : "s"}`,
    );
  });

  await test.step("Escape hands the keyboard back to whatever opened it", async () => {
    // Cancelling a gesture must not dump the user at the top of the document.
    // A row that *runs* is the other case and deliberately does not restore:
    // the command it ran owns focus from there (a new terminal takes it).
    await page.keyboard.press("Escape");
    await expect(palette(page)).toBeHidden();

    const invoker = treeItem(page, "src");
    await invoker.click();
    await expect(invoker).toBeFocused();

    await page.keyboard.press("Control+P");
    await expect(palette(page)).toBeVisible();
    await expect(palette(page).locator(".wb-qb-input")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(palette(page)).toBeHidden();
    await expect(invoker).toBeFocused();
  });
});

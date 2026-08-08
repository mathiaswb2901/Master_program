/**
 * ANVIL V4, live — the ambient layer in a real browser.
 *
 * `statusChrome.test.ts` asserts what the stylesheets *declare*; this asserts
 * what the browser *computed*, which is the only place one particular class of
 * bug shows itself: a `var(--token)` that resolves to nothing. An undefined
 * custom property with no fallback is valid CSS, so the rule silently does not
 * apply — the exact bug `tokenRefs.test.ts` was written for after a search
 * highlight painted with a token nobody had defined. Reading the value back
 * through a probe element in the live page is what closes it end to end: the
 * probe resolves `var(--warn)` the way the toast's own border does, so the two
 * either agree or the token is not doing anything.
 *
 * Deliberately *not* a screenshot. A pixel diff on a surface with a live
 * session count and a machine-dependent font stack fails for reasons that have
 * nothing to do with the design, and says nothing about which rule broke.
 *
 * The warn toast this drives is the one the seeded `.workbench/shortcuts.md`
 * raises on every load (see `quickbar.spec.ts`) — a real notification on the
 * real path, not one poked into the store. It auto-dismisses ~6 s later, so the
 * toast assertions come first and take no detours.
 */

import { expect, test, type Page } from "@playwright/test";

import { gotoApp, workspaceReady } from "./app";

/**
 * What `var(<token>)` computes to *in this page*, as a normalised `rgb(...)`.
 *
 * A probe rather than reading the custom property off `:root`: the declared
 * value is a hex string and the value under test is a computed colour, so they
 * cannot be compared without re-implementing the browser's parsing. Resolving
 * it the way the toast's own border does keeps both sides honest — and a token
 * that does not exist resolves to the page's inherited text colour, which is
 * `--text-primary` and matches none of the semantics asserted below.
 */
async function tokenColor(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const computed = getComputedStyle(probe).color;
    probe.remove();
    return computed;
  }, token);
}

test("the status bar and the toast layer resolve their tokens", async ({ page }) => {
  await gotoApp(page);

  await test.step("a warn toast says its kind with a colour AND a shape (§7)", async () => {
    const toast = page.locator(".wb-toast.is-warn").first();
    await expect(toast).toBeVisible();

    // The 2px semantic edge is the tint...
    const edge = await toast.evaluate((el) => getComputedStyle(el).borderLeftColor);
    expect(edge).toBe(await tokenColor(page, "--warn"));

    // ...the 14px mark is the shape, in the same semantic, and it is a real
    // rendered glyph rather than a class name nobody drew.
    const mark = toast.locator(".wb-toast-mark");
    await expect(mark).toBeVisible();
    expect(await mark.evaluate((el) => getComputedStyle(el).color)).toBe(
      await tokenColor(page, "--warn"),
    );

    // ...and the kind is spoken, for the reader who gets neither.
    await expect(toast).toContainText("Warning");
  });

  await test.step("the toast layer takes no click it does not own", async () => {
    // The column is 360px wide and taller than its toasts by its gaps; a
    // transparent region of it swallowing clicks on the panel underneath is the
    // bug `pointer-events: none` on the container prevents.
    const layer = page.locator(".wb-toasts");
    expect(await layer.evaluate((el) => getComputedStyle(el).pointerEvents)).toBe("none");
    expect(
      await page.locator(".wb-toast").first().evaluate((el) => getComputedStyle(el).pointerEvents),
    ).toBe("auto");
  });

  await workspaceReady(page);

  await test.step("the bar is the 24px readout on the chrome surface (§6.7)", async () => {
    const bar = page.locator(".wb-statusbar");
    const computed = await bar.evaluate((el) => {
      const style = getComputedStyle(el);
      return {
        height: el.getBoundingClientRect().height,
        background: style.backgroundColor,
        figures: style.fontVariantNumeric,
        fontSize: style.fontSize,
      };
    });
    expect(computed.height).toBe(24);
    expect(computed.background).toBe(await tokenColor(page, "--surface-app"));
    expect(computed.figures).toContain("tabular-nums");
    expect(computed.fontSize).toBe("11px");
  });

  await test.step("a reading that is on screen is never zero — the quiet bar", async () => {
    // Scoped by value, not by count: the suite shares one backend, so how many
    // sessions are live when this runs is not this journey's business. What is,
    // is that nothing in the bar is displaying a nought.
    const readings = await page.locator(".wb-status-count").allInnerTexts();
    expect(readings.filter((text) => /(^|\D)0(\D|$)/.test(text))).toEqual([]);
  });

  await test.step("an emptied region leaves no gap behind it", async () => {
    // `display: none` on `:empty` is what makes "hide at zero" true of the bar
    // and not only of the items in it.
    const widths = await page.locator(".wb-statusbar > div").evaluateAll((nodes) =>
      nodes.map((node) => ({
        empty: node.childElementCount === 0,
        width: node.getBoundingClientRect().width,
      })),
    );
    expect(widths.length).toBe(3);
    expect(widths.filter((region) => region.empty && region.width > 0)).toEqual([]);
  });
});

/**
 * Journey 9 — a visual artifact, end to end.
 *
 * The fake agent presents a plan whose one node is a `visual`: every leaf kind,
 * every block layout, on a real 25-hour market day (`services/fake_agent.py`).
 *
 * Asserts, in the order they would break:
 *  - the card renders every leaf natively — a table with tabular figures on the
 *    column the *schema* typed numeric, an SVG chart whose step series is drawn
 *    as steps, a diagram we laid out from nodes with no coordinates, a diff,
 *    and a row of metrics;
 *  - no leaf lets colour be the only signal (DESIGN.md §7): a roled diagram box
 *    and a roled metric wear a heavier edge than their neutral neighbours *and*
 *    name the role in words. Computed style is only real in a browser, so this
 *    is the one place the visible half of that pair can be asserted;
 *  - the DST day is drawn and labelled as 25 hours in the market's own zone —
 *    the assertion a generic time axis passes on 363 days a year and fails on
 *    this one;
 *  - a cell whose text looks like markup renders as **text**: no script, no
 *    element, nothing added to the document;
 *  - **zero network requests** are issued while the artifact renders — recorded
 *    from before the message that produces it until it is on screen, which is
 *    exactly the window the claim is about. That is the property that made us
 *    build this instead of adopting a third-party artifact bridge, and the only
 *    place it can be proven is in a real browser;
 *  - the decision still round-trips to the agent, so a drawn card is a card.
 */

import { expect, test, type Request } from "@playwright/test";

import { assistantBlocks, newSession, openApp, sendChat } from "./app";

test("visual artifact: drawn natively, DST-correct, inert, and answerable", async ({ page }) => {
  await openApp(page);
  await newSession(page);

  // Every request the page makes, recorded from before the message that
  // produces the artifact until the artifact is on screen. The listener comes
  // off at that point on purpose: the window has to be *the render*, and the
  // seconds of inspection that follow are ordinary app life, in which Workbench's
  // own control plane is entitled to talk (`Layouts.tsx` debounces an arrangement
  // to `PUT /api/layouts` 500 ms after any dock change). Recording past the render
  // would eventually catch one of those and call it an artifact fetching something.
  const requests: string[] = [];
  const record = (request: Request): void => {
    requests.push(`${request.method()} ${request.url()}`);
  };
  page.on("request", record);

  await sendChat(page, "visual please");

  const card = page.locator(".wb-plan-card");
  const visual = card.locator(".wb-vis");
  await expect(card).toBeVisible();
  await expect(visual).toBeVisible();
  page.off("request", record);

  await test.step("rendering the artifact issued no network request", () => {
    // Not "no image request" — none at all, over the whole send-and-draw. A
    // visual payload has no field we could fetch, and the renderer has no code
    // that would.
    expect(requests, `unexpected requests: ${requests.join(", ")}`).toEqual([]);
  });

  await test.step("every leaf kind rendered natively", async () => {
    await expect(visual.locator(".wb-vis-metrics")).toHaveCount(1);
    await expect(visual.locator("svg.wb-vis-chart")).toHaveCount(1);
    await expect(visual.locator("table.wb-vis-table")).toHaveCount(2);
    await expect(visual.locator("svg.wb-vis-diagram")).toHaveCount(1);
    await expect(visual.locator(".wb-vis-diff")).toHaveCount(1);
    // Blocks: single, single, split, row — the layouts, not a stack of divs.
    await expect(visual.locator(".wb-vis-block.is-split")).toHaveCount(1);
    await expect(visual.locator(".wb-vis-block.is-row")).toHaveCount(1);
  });

  await test.step("the schema, not the text, decides the presentation", async () => {
    const price = visual.locator("table.wb-vis-table td.is-numeric").first();
    await expect(price).toHaveCSS("font-variant-numeric", "tabular-nums");
    await expect(price).toHaveCSS("text-align", "right");
    // A highlighted cell is tinted *and* carries its role word for a reader who
    // cannot see the tint (DESIGN.md §7).
    await expect(visual.locator("td.is-error")).toHaveCount(1);
    await expect(visual.locator("td.is-error")).toContainText("Problem");
  });

  await test.step("no leaf lets colour be the only signal", async () => {
    // DESIGN.md §7. The role vocabulary is shared across leaves, so the *pair*
    // has to be too: a word for a reader who cannot see the tint, and an edge
    // heavier than the same element wears with no role for one who can see it
    // but not the hue. The weights are CSS, so this is the only place they are
    // real — vitest sees markup, not computed style.
    const weights = await page.evaluate(() => {
      const px = (selector: string, property: string): number => {
        const element = document.querySelector(selector);
        if (element === null) return -1;
        return parseFloat(getComputedStyle(element).getPropertyValue(property));
      };
      return {
        // `[class="wb-vis-node"]` is the node with no role at all.
        plainBox: px('svg.wb-vis-diagram g[class="wb-vis-node"] rect', "stroke-width"),
        roledBox: px("svg.wb-vis-diagram g.wb-vis-node.is-accent rect", "stroke-width"),
        plainFigure: px('.wb-vis-metric[class="wb-vis-metric"]', "border-left-width"),
        roledFigure: px(".wb-vis-metric.is-accent", "border-left-width"),
      };
    });
    expect(weights.plainBox).toBeGreaterThan(0);
    expect(weights.roledBox).toBeGreaterThan(weights.plainBox);
    expect(weights.plainFigure).toBeGreaterThan(0);
    expect(weights.roledFigure).toBeGreaterThan(weights.plainFigure);

    // An `svg` with role="img" has one accessible name, so a diagram node's
    // role word lives there; a metric carries its own in a `u-sr-only` span.
    await expect(visual.locator("svg.wb-vis-diagram")).toHaveAttribute(
      "aria-label",
      /Optimizer \(Highlighted\).*Bid file \(Good\).*Gate 12:00 \(Warning\)/,
    );
    await expect(visual.locator(".wb-vis-metric.is-warning")).toContainText(
      "Warning: Negative hours",
    );
  });

  await test.step("the 25-hour day is drawn as 25 hours in the market's clock", async () => {
    const caption = visual.locator(".wb-vis-grid-caption");
    await expect(caption).toHaveText("25 Oct · Europe/Stockholm · 25 h");
    // Ticks land on grid points, so no label ever names an instant the day does
    // not contain. How *many* there are depends on the panel's width — the
    // Agent panel is whatever the previously-restored layout left it — so this
    // asserts the shape of every label, not a count; the exhaustive DST cases
    // (missing 02:00, duplicated 02:00) live in `visual/timeAxis.test.ts`.
    const ticks = await visual.locator("svg.wb-vis-chart text.is-x").allTextContents();
    expect(ticks.length).toBeGreaterThanOrEqual(2);
    expect(ticks[0]).toBe("00:00");
    expect(ticks.every((tick) => /^\d{2}:\d{2}$/.test(tick))).toBe(true);
    // A step series holds each value across its interval: its path has two
    // points per value, which a line series never does.
    const paths = await visual.locator("svg.wb-vis-chart path.wb-vis-line").count();
    expect(paths).toBe(2);
  });

  await test.step("markup in a cell is text, and nothing was added to the page", async () => {
    const cell = visual.locator("td", { hasText: "<script>" });
    await expect(cell).toHaveCount(1);
    await expect(cell).toHaveText("<script>alert('xss')</script>");
    const inert = await page.evaluate(() => {
      const root = document.querySelector(".wb-vis");
      return {
        scripts: document.querySelectorAll("script[src*='xss'], script:not([type])").length,
        embedded:
          root?.querySelectorAll("img, iframe, object, embed, a, form, script").length ?? -1,
        handlers: root?.querySelectorAll("[onclick], [onerror], [onload]").length ?? -1,
        alerted: "__xss__" in window,
      };
    });
    expect(inert.embedded).toBe(0);
    expect(inert.handlers).toBe(0);
    expect(inert.alerted).toBe(false);
  });

  await test.step("a drawn card is still a card the agent hears back from", async () => {
    await card.getByRole("button", { name: "Approve" }).click();
    await expect(card.locator(".wb-plan-verdict")).toHaveText("Approved");
    await expect(assistantBlocks(page).last()).toContainText("plan approve: no choices");
  });
});

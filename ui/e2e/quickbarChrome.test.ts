/**
 * ANVIL V3 — the QuickBar's chrome, as a contract (DESIGN.md §6.5, §5.4, §2.4).
 *
 * A vitest test next to `paneChrome.test.ts` and `emptyState.test.ts`, and for
 * the same reason: the questions here are about what the stylesheet *declares* —
 * which surface the overlay floats on, whether the selected row is the only
 * amber in it, whether anything animates a selection that tracks the arrow key —
 * and those are answered off disk in milliseconds. The look itself is asserted in
 * a real browser by `quickbarChrome.spec.ts`; the behaviour by `quickbar.spec.ts`,
 * which V3 left untouched on purpose.
 *
 * The load-bearing one is the amber census. This overlay is the surface every
 * command is run through, so it is where a decorative amber would be seen most
 * and would cost the most: §2.4 spends it on *where I am* and *changing right
 * now*, which here is the row Enter would run and the caret in the field being
 * typed into — and nothing else, ever.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { rules, type CssRule } from "./perf/css";

const STYLES = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "src", "styles");

const QUICKBAR: CssRule[] = rules(fs.readFileSync(path.join(STYLES, "quickbar.css"), "utf-8"));

/** The declarations of the rule whose selector is exactly this. */
function ruleExactly(selector: string): string {
  const found = QUICKBAR.find((rule) => rule.selector === selector);
  expect(found, `no rule with selector "${selector}"`).toBeDefined();
  return String(found?.body);
}

describe("the overlay is §6.5's surface, drawn from tokens", () => {
  const bar = ruleExactly(".wb-qb");

  it("floats on --surface-overlay at elevation 3, with the hairline every shadow owes (§4)", () => {
    expect(bar).toMatch(/background:\s*var\(--surface-overlay\)/);
    expect(bar).toMatch(/box-shadow:\s*var\(--shadow-3\)/);
    expect(bar).toMatch(/border:\s*1px solid var\(--border-default\)/);
    expect(bar).toMatch(/border-radius:\s*var\(--radius-xl\)/);
  });

  it("takes its 640px width from the token, not a number in a stylesheet", () => {
    expect(bar).toMatch(/width:\s*var\(--quickbar-width\)/);
    expect(bar).toMatch(/max-height:\s*60vh/);
  });

  it("keeps the rows at §6.5's 40px, through the same token the geometry test pins", () => {
    expect(ruleExactly(".wb-qb-row")).toMatch(/height:\s*var\(--row-height-lg\)/);
  });
});

describe("the amber is spent on the selected row and the caret, and nowhere else", () => {
  it("marks the row Enter would run — the wash plus a 2px left edge (§6.5)", () => {
    const selected = ruleExactly(".wb-qb-row.is-selected");
    expect(selected).toMatch(/background:\s*var\(--surface-selected\)/);
    expect(selected).toMatch(/border-left-color:\s*var\(--accent\)/);
    // The 2px is on the base row so selection never changes the row's box —
    // an edge that appeared would shift every label 2px as the arrow moved.
    expect(ruleExactly(".wb-qb-row")).toMatch(/border-left:\s*2px solid transparent/);
  });

  it("answers a hover with the wash, never the one colour that means 'here' (§2.4)", () => {
    const hover = ruleExactly(".wb-qb-row:hover:not(:disabled):not(.is-selected)");
    expect(hover).toMatch(/background:\s*var\(--surface-hover\)/);
    expect(hover).not.toMatch(/--accent|--surface-selected/);
  });

  it("spends nothing on the match highlight — that is a hierarchy, not a colour", () => {
    // A highlight is on every visible row at once. Amber there is exactly the
    // "standing decoration" §2.4 forbids, and it would drown the selected row.
    for (const selector of [
      ".wb-qb-row-title.is-matched",
      ".wb-qb-row-title .wb-qb-hit",
      ".wb-qb-row-detail .wb-qb-hit",
    ]) {
      expect(ruleExactly(selector), selector).not.toMatch(/--accent|--focus-ring/);
    }
    expect(ruleExactly(".wb-qb-row-title .wb-qb-hit")).toMatch(/color:\s*var\(--text-primary\)/);
  });

  it("censuses every --accent in the file: the selected edge and the caret (§2.4)", () => {
    const spends = QUICKBAR.filter((rule) => /var\(--accent\b/.test(rule.body)).map(
      (rule) => rule.selector,
    );
    expect(spends.sort()).toEqual([".wb-qb-input", ".wb-qb-row.is-selected"]);
    // And the input's is the caret — "the field you are typing in", not a rule,
    // a border or a standing mark. The focus underline was demoted to
    // --border-strong when ANVIL landed and must not come back.
    expect(ruleExactly(".wb-qb-input")).toMatch(/caret-color:\s*var\(--accent\)/);
    expect(ruleExactly(".wb-qb-input:focus-visible")).toMatch(
      /border-bottom-color:\s*var\(--border-strong\)/,
    );
  });
});

describe("nothing in here animates a selection (§5.4)", () => {
  it("puts no transition on a result row, its hover or its selected state", () => {
    // Selection tracks the arrow key, so it is in §5.4's "does not move" column:
    // a wash that eases in behind ↓ is the feeling of lag, and it is the single
    // most likely thing for a later polish pass to add.
    for (const rule of QUICKBAR) {
      if (!rule.selector.startsWith(".wb-qb-row")) continue;
      expect(rule.body, `${rule.selector} must not transition`).not.toMatch(/transition/);
    }
  });

  it("keeps the entrance on the shared tokens, travel expressed as --motion-scale-in", () => {
    expect(ruleExactly(".wb-qb")).toMatch(/animation:\s*wb-qb-in var\(--motion-enter\)/);
    const from = QUICKBAR.find(
      (rule) => rule.selector === "from" && rule.body.includes("--motion-scale-in"),
    );
    expect(from, "wb-qb-in scales from --motion-scale-in, so reduced motion zeroes it").toBeDefined();
  });

  it("still leaves rather than vanishing, scrim and all (§5.4, motion.spec.ts)", () => {
    const leaving = ruleExactly(".wb-qb.is-leaving, .wb-qb-backdrop.is-leaving");
    expect(leaving).toMatch(/animation:\s*wb-fade-out var\(--motion-exit\)/);
    expect(leaving).toMatch(/pointer-events:\s*none/);
  });

  it("brings the scrim in with the bar, on opacity alone", () => {
    // The scrim used to snap to 60% black a frame before the surface above it
    // had finished arriving, which is the part of this gesture that read as a
    // jump. Opacity only: the tint channel, untouched by reduced motion.
    expect(ruleExactly(".wb-qb-backdrop")).toMatch(
      /animation:\s*wb-qb-scrim-in var\(--motion-enter\)/,
    );
    const steps = QUICKBAR.filter((rule) => rule.selector === "from" || rule.selector === "to");
    for (const step of steps) {
      expect(step.body).not.toMatch(/background|width|height|top|left/);
    }
  });
});

describe("the list stays readable and stays put", () => {
  it("pins a section header, because in a pick the sections are the vocabulary (§6.11)", () => {
    const cat = ruleExactly(".wb-qb-cat");
    expect(cat).toMatch(/position:\s*sticky/);
    // On the bar's own surface, so a row passing under it is covered rather
    // than blended into a second, unexplained layer.
    expect(cat).toMatch(/background:\s*var\(--surface-overlay\)/);
    expect(cat).toMatch(/color:\s*var\(--text-tertiary\)/);
  });

  it("keeps the arrowed-to row clear of that header", () => {
    // `scrollIntoView({ block: "nearest" })` stops as soon as the row's box is
    // inside the viewport — under a sticky header, that is the moment it is
    // hidden by one. The margin is the header's own box, from its own tokens.
    const row = ruleExactly(".wb-qb-row").replace(/\s+/g, " ");
    expect(row).toContain(
      "scroll-margin-top: calc( var(--space-3) + var(--space-4) + var(--type-xs-line) + " +
        "var(--space-2) + var(--space-1) )",
    );
  });

  it("contains its overscroll, so the dock behind the scrim does not scroll", () => {
    expect(ruleExactly(".wb-qb-results")).toMatch(/overscroll-behavior:\s*contain/);
  });

  it("dims a row that cannot run, keycaps included (§6.5)", () => {
    expect(ruleExactly(".wb-qb-row:disabled")).toMatch(/color:\s*var\(--text-disabled\)/);
    expect(ruleExactly(".wb-qb-row:disabled .wb-keycap")).toMatch(/color:\s*var\(--text-disabled\)/);
    expect(ruleExactly(".wb-qb-row:disabled .wb-qb-hit")).toMatch(/color:\s*var\(--text-disabled\)/);
  });
});

describe("no raw hex — every colour is a token (house rule, §2)", () => {
  it("holds across the whole QuickBar stylesheet", () => {
    const offenders = QUICKBAR.filter((rule) => /#[0-9a-fA-F]{3,8}\b/.test(rule.body)).map(
      (rule) => rule.selector,
    );
    expect(offenders).toEqual([]);
  });
});

/**
 * ANVIL V1 — the dock, tab strip and pane chrome, made into a gate.
 *
 * The frame the user sees first is composition, not colour: the palette landing
 * (#59) proved every hue against `palette.test.ts`, and this asserts the *frame*
 * built on it stays cohesive and restrained. Like `palette.test.ts` it reads the
 * stylesheets off disk rather than a copy of their values, so the document and
 * the CSS cannot drift apart without the build saying so. It lives under `e2e/`
 * for the same reason — files outside the `tsc -b` program may read from disk —
 * and is a `*.test.ts`, so it runs under vitest in milliseconds (DESIGN.md §6.1,
 * §6.11).
 *
 * The one thing it guards above every other: the amber is spent only where
 * DESIGN.md §2.4 allows it in the frame — the live rule, the drop target, the
 * dragged sash — and nowhere a hover or an inactive state could leak it in. A
 * tab that lit amber on hover would be the exact failure the whole identity
 * exists to prevent: the one colour that means "here" and "now" reduced to
 * decoration.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { rules, type CssRule } from "./perf/css";

const STYLES = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "src", "styles");

const sheet = (name: string): CssRule[] =>
  rules(fs.readFileSync(path.join(STYLES, name), "utf-8"));

/** The declarations of the first rule whose selector contains every fragment. */
function ruleContaining(sheetRules: CssRule[], ...fragments: string[]): CssRule {
  const found = sheetRules.find((rule) => fragments.every((f) => rule.selector.includes(f)));
  expect(found, `no rule matching ${fragments.join(" + ")}`).toBeDefined();
  return found as CssRule;
}

/** The declarations of the rule whose selector is exactly this — needed where a
 * longer selector (`.wb-pane-actions`) would match a substring probe for a
 * shorter one (`.wb-pane-action`). */
function ruleExactly(sheetRules: CssRule[], selector: string): CssRule {
  const found = sheetRules.find((rule) => rule.selector === selector);
  expect(found, `no rule with selector "${selector}"`).toBeDefined();
  return found as CssRule;
}

// ---- the dock + tab strip (dockview.css) ------------------------------------

describe("the frame is painted from the tokens, inverted ramp and all", () => {
  const dock = sheet("dockview.css");
  const theme = ruleContaining(dock, ".dockview-theme-workbench").body;

  it("paints the pane tab strip at chrome value (--surface-app), lighter than the panel it frames", () => {
    // The whole ANVIL direction: chrome is the *lighter* surface (DESIGN.md
    // §2.1). If this strip ever reads --surface-panel the frame has collapsed
    // back into a graphite editor.
    expect(theme).toMatch(/--dv-tabs-and-actions-container-background-color:\s*var\(--surface-app\)/);
  });

  it("fuses the active pane tab into the panel body below it (--surface-panel)", () => {
    // The active tab's background is the surface immediately below it, which for
    // a pane tab is the panel — that fusion is the active indicator (§6.1), not
    // an underline or an accent bar.
    expect(theme).toMatch(
      /--dv-activegroup-visiblepanel-tab-background-color:\s*var\(--surface-panel\)/,
    );
  });

  it("gives the dock gap the strong hairline, not the subtle one (§2.1)", () => {
    // The sash sits in the `--surface-app` gap, where `--border-subtle` lands at
    // 1.78:1 and disappears; the most structural line in the window gets the
    // weight that makes it visible where it actually sits.
    expect(theme).toMatch(/--dv-separator-border:\s*var\(--border-strong\)/);
  });
});

describe("the live rule — the signature — is the amber, 2px, full-bleed", () => {
  const dock = sheet("dockview.css");
  const before = ruleContaining(
    dock,
    ".dv-active-group",
    ".dv-tabs-and-actions-container::before",
  ).body;

  it("is 2px of --accent across the top of the focused pane's strip (§6.1)", () => {
    expect(before).toMatch(/background:\s*var\(--accent\)/);
    expect(before).toMatch(/height:\s*2px/);
  });

  it("is not animated — a focus mark that arrives late lies (§6.1)", () => {
    // It is a static overlay; nothing may put a transition on it.
    expect(before).not.toMatch(/transition/);
  });
});

describe("the tab states are complete, and only the sanctioned ones carry amber", () => {
  const dock = sheet("dockview.css");
  const hover = ruleContaining(dock, ".dv-tab.dv-inactive-tab:hover").body;
  const focus = ruleContaining(dock, ".dv-tab:focus-visible").body;

  it("answers an inactive-tab hover with the wash and a text step, never the amber (§2.4)", () => {
    expect(hover).toMatch(/background-color:\s*var\(--surface-hover\)/);
    expect(hover).toMatch(/color:\s*var\(--text-secondary\)/);
    // The load-bearing assertion of this whole file: a hover must not spend the
    // one colour that means "here". Amber on hover is the identity's failure mode.
    expect(hover).not.toMatch(/--accent/);
  });

  it("gives a keyboard-focused tab a visible 2px focus ring (§1.8)", () => {
    expect(focus).toMatch(/outline:\s*2px solid var\(--focus-ring\)/);
  });
});

// ---- the pane system (panes.css) --------------------------------------------

describe("the pane chrome moves on the one motion vocabulary", () => {
  const panes = sheet("panes.css");

  it("tints the split affordance on the tint channel, not a bare legacy duration (§5.3)", () => {
    const action = ruleExactly(panes, ".wb-pane-action").body;
    // The colour transition rides `--motion-tint`; nothing here travels, and no
    // deprecated `--duration-1` / `--ease-standard` pair survives in the frame.
    expect(action).toMatch(/transition:\s*[\s\S]*var\(--motion-tint\)/);
    expect(action).not.toMatch(/--duration-1|--ease-standard/);
  });

  it("lights the affordance instantly on hover and eases only the decay (§5.5)", () => {
    const hover = ruleExactly(panes, ".wb-pane-action:hover").body;
    expect(hover).toMatch(/transition-duration:\s*0s/);
  });

  it("brings a new pane in on opacity and a zoom, the one panel-level movement (§6.11)", () => {
    // Travel is a token so reduced motion zeroes it (tokens.css) — the frame
    // must express the scale through `--motion-zoom-in`, never a hard number.
    const keyframe = panes.find(
      (rule) => rule.selector === "from" && rule.body.includes("--motion-zoom-in"),
    );
    expect(keyframe, "the wb-pane-in entrance keyframe scales from --motion-zoom-in").toBeDefined();
  });
});

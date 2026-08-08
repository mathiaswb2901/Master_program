/**
 * ANVIL V2 — the empty-state anatomy and the welcome card, as a contract
 * (DESIGN.md §6.10, §6.13).
 *
 * A vitest test rather than a Playwright one, next to `perf/motion.test.ts` and
 * for the same reason: the questions here are about what a stylesheet *declares*
 * — which class carries the app's entrance, whether the one empty-state action is
 * an outline and not a fill, whether the welcome card spends its single amber on
 * the marker §2.4 allows it — and those are answered off disk in milliseconds. It
 * reuses the perf lane's tiny CSS reader so "a rule" means the same thing here as
 * it does to the motion budget. The look itself is asserted in the browser by
 * `discover.spec.ts`; this is the structure that look is built on.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { rules } from "./perf/css";

const STYLE_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "src", "styles");

function read(name: string): string {
  return fs.readFileSync(path.join(STYLE_ROOT, name), "utf-8");
}

/** One rule's declarations, whitespace as `rules()` reports it. */
function body(css: string, selector: string): string {
  const found = rules(css).find((rule) => rule.selector === selector);
  expect(found, `expected a rule for \`${selector}\``).toBeDefined();
  return String(found?.body);
}

const APP = read("app.css");
const KEYBOARD = read("keyboard.css");

describe("the shared empty-state anatomy (§6.10)", () => {
  it("enters on the app's one entrance, not a bespoke animation", () => {
    // The same `wb-rise-in` keyframe every panel-level surface uses, driven by
    // `--motion-enter` — so reduced motion turns it into a pure fade for free.
    expect(body(APP, ".wb-empty")).toContain("animation: wb-rise-in var(--motion-enter)");
  });

  it("frames a 32px, tertiary icon slot", () => {
    const icon = body(APP, ".wb-empty-icon");
    expect(icon).toContain("color: var(--text-tertiary)");
    const glyph = body(APP, ".wb-empty-icon svg");
    expect(glyph).toContain("width: 32px");
    expect(glyph).toContain("height: 32px");
  });

  it("caps the text column at 260px", () => {
    expect(body(APP, ".wb-empty-title, .wb-empty-hint")).toContain("max-width: 260px");
  });

  it("offers one action that is an outline, never a fill (§6.10)", () => {
    const action = body(APP, ".wb-empty-action");
    expect(action).toContain("border: 1px solid var(--border-default)");
    // A filled call to action is exactly what §6.10 forbids an empty state.
    expect(action).not.toContain("var(--accent-fill)");
  });

  it("answers hover on the frame it arrives — instant in, eased out (§5)", () => {
    expect(body(APP, ".wb-empty-action:hover, .wb-empty-action:active")).toContain(
      "transition-duration: 0s",
    );
  });

  it("keeps the link form on the amber §6.10 allows, and underlines it (§7)", () => {
    const link = body(APP, ".wb-empty-action-link");
    expect(link).toContain("color: var(--accent)");
    expect(link).toContain("text-decoration: underline");
  });
});

describe("the welcome card — the product's most important empty state (§6.13)", () => {
  it("enters on the app's entrance", () => {
    expect(body(KEYBOARD, ".wb-welcome")).toContain("animation: wb-rise-in var(--motion-enter)");
  });

  it("spends its single amber on the marker §2.4 allows *where I am*", () => {
    const marker = body(KEYBOARD, ".wb-welcome::before");
    expect(marker).toContain('content: ""');
    expect(marker).toContain("background: var(--accent)");
  });

  it("still runs its affordances as colour-only, instant-in hovers (§5)", () => {
    // V2 dresses the card, it does not change how the rows behave.
    expect(body(KEYBOARD, ".wb-welcome-action:hover, .wb-welcome-action:active")).toContain(
      "transition-duration: 0s",
    );
  });
});

describe("no raw hex — every colour is a token (house rule, §2)", () => {
  it("holds across the whole empty-state and welcome surface", () => {
    // Belt-and-braces beside `palette.test.ts`: a `#RRGGBB` slipping into the one
    // surface V2 owns is caught in this lane's own test, at vitest speed.
    for (const [file, css] of [
      ["app.css", APP],
      ["keyboard.css", KEYBOARD],
    ] as const) {
      const offenders = rules(css)
        .filter((rule) => /\.wb-empty|\.wb-welcome/.test(rule.selector))
        .filter((rule) => /#[0-9a-fA-F]{3,8}\b/.test(rule.body))
        .map((rule) => `${file}  ${rule.selector}`);
      expect(offenders).toEqual([]);
    }
  });
});

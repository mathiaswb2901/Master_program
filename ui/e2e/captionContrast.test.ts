/**
 * The one claim the caption tint makes about the palette: the window title is
 * readable on the caption, in both themes.
 *
 * It is a claim about *token values*, not about a rendered document — Windows
 * paints the caption from three literal colours we hand it, so the stylesheet
 * is the thing to check. Hence `node:fs` and hence this directory: a unit test
 * under `src/` is inside `tsc -b`'s program, which has no `@types/node`, and a
 * CSS import gives vitest an empty module. `perf/rowGeometry.test.ts` reads
 * tokens the same way and for the same reason; vitest owns `*.test.ts` here
 * while `*.spec.ts` stays Playwright's.
 *
 * Nothing else in the suite can see this. No test drives a native caption, so a
 * palette change that made `--text-secondary` vanish into `--surface-app` would
 * ship green — and a palette change is exactly what this file was written
 * ahead of. DESIGN.md §7: body text ≥ 4.5:1 on its surface, and "verify any new
 * pair before adding it".
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/** Kept in step with `CAPTION_TOKENS` in `src/captionTint.ts` by the test below. */
const CAPTION_TOKENS = {
  caption: "--surface-app",
  text: "--text-secondary",
  border: "--border-strong",
};

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..");

const read = (relative: string): string =>
  fs.readFileSync(path.join(UI_ROOT, ...relative.split("/")), "utf-8");

/** Every `--token: value;` inside the first block matching `selector`. */
function tokenBlock(css: string, selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  expect(start, `${selector} is missing from tokens.css`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  const block: Record<string, string> = {};
  for (const [, name, value] of css.slice(open, close).matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    block[name] = value.trim();
  }
  return block;
}

/** `#RRGGBB` / `#RRGGBBAA` → lowercase `#rrggbb`, or null. Mirrors `opaqueHex`. */
function opaqueHex(value: string | undefined): string | null {
  const match = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec((value ?? "").trim());
  return match === null ? null : `#${match[1].toLowerCase()}`;
}

/** WCAG 2.1 relative luminance of a hex colour. */
function luminance(hex: string): number {
  const value = opaqueHex(hex);
  expect(value, `${hex} is not a hex colour`).not.toBeNull();
  const channel = (at: number): number => {
    const c = parseInt((value as string).slice(at, at + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

/** WCAG 2.1 contrast ratio, 1:1 to 21:1. */
function contrast(a: string, b: string): number {
  const [dark, light] = [luminance(a), luminance(b)].sort((x, y) => x - y);
  return (light + 0.05) / (dark + 0.05);
}

describe("the native caption's colours", () => {
  const css = read("src/design/tokens.css");
  // Dark is `:root`; light overrides on the attribute (tokens.css header).
  const themes = {
    dark: tokenBlock(css, ":root {"),
    light: tokenBlock(css, '[data-theme="light"] {'),
  };

  it("computes contrast the way WCAG does", () => {
    // The helper is the whole basis of the assertions below, so it is pinned to
    // the two ratios everyone knows before it is trusted with the palette.
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
  });

  it("names the same three tokens the module does", () => {
    // This file cannot import `src/captionTint.ts` — it would drag `shell.ts`
    // and `@tauri-apps/api` into a node test for three strings — so the copy
    // above is checked against the source instead.
    const module = read("src/captionTint.ts");
    for (const [part, token] of Object.entries(CAPTION_TOKENS)) {
      expect(module).toMatch(new RegExp(`${part}:\\s*"${token}"`));
    }
  });

  for (const [name, tokens] of Object.entries(themes)) {
    it(`keeps the title ≥ 4.5:1 on the caption (${name})`, () => {
      const caption = tokens[CAPTION_TOKENS.caption];
      const text = tokens[CAPTION_TOKENS.text];
      expect(caption, `${name} has no ${CAPTION_TOKENS.caption}`).toBeDefined();
      expect(text, `${name} has no ${CAPTION_TOKENS.text}`).toBeDefined();
      // The window title is body-sized text on the caption surface, so it is
      // the 4.5:1 rule rather than the 3:1 one for glyphs. Measured today:
      // 8.3:1 dark, 6.5:1 light.
      expect(contrast(caption, text)).toBeGreaterThanOrEqual(4.5);
    });

    it(`resolves every frame token to an opaque colour (${name})`, () => {
      // A Win32 `COLORREF` has no alpha, and half the surface tokens are
      // `rgba(…)` — a frame token that became one would be silently flattened
      // by the shell rather than refused. Border deliberately carries no ratio
      // requirement: it separates the window from an unknown desktop, not from
      // its own caption, and it is a hairline by design (DESIGN.md §1.4).
      for (const token of Object.values(CAPTION_TOKENS)) {
        expect(opaqueHex(tokens[token]), `${name} ${token}`).not.toBeNull();
      }
    });
  }
});

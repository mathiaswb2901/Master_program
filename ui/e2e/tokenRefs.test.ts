/**
 * Every `var(--token)` a stylesheet reads must be a token something defines.
 *
 * This exists because of a bug that shipped past review and past every other
 * gate: `search.css` painted the matched-text `<mark>` with `var(--accent-tint)`,
 * a token that does not exist. An undefined `var()` with no fallback resolves to
 * nothing, so the rule silently does nothing — the highlight simply never
 * appeared, in either theme. Nothing else can catch it: it is valid CSS, `tsc`
 * never reads a stylesheet, eslint never reads a stylesheet, and a component
 * test asserts the `<mark>` exists rather than that it is visible.
 *
 * So the check is source-level and repo-wide: collect the custom properties
 * `tokens.css` (and the sheet itself) define, collect what each sheet reads, and
 * fail on the difference. It lives under `e2e/` for the same reason
 * `palette.test.ts` does — reading the real `tokens.css` off disk, rather than a
 * copy of its values, is the whole point, and files outside `tsc -b`'s program
 * are the ones allowed to touch the filesystem.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..");
const STYLES = path.join(UI_ROOT, "src", "styles");
const TOKENS = path.join(UI_ROOT, "src", "design", "tokens.css");

/**
 * Custom properties supplied at runtime from a component's inline `style`,
 * which no stylesheet can declare.
 *
 * Each entry names the module that sets it, so an addition here is a claim
 * someone can check rather than a way to silence the test.
 */
const SET_FROM_TSX: ReadonlySet<string> = new Set([
  "--pill-color", // panels/ReviewPanel.tsx, panels/ObjectivePanel.tsx
  "--pill-bg", //    panels/ReviewPanel.tsx, panels/ObjectivePanel.tsx
]);

const DEFINITION = /(--[a-zA-Z0-9-]+)\s*:/g;
const REFERENCE = /var\(\s*(--[a-zA-Z0-9-]+)/g;

const withoutComments = (css: string): string => css.replace(/\/\*[\s\S]*?\*\//g, "");

const namesMatching = (css: string, pattern: RegExp): Set<string> =>
  new Set([...withoutComments(css).matchAll(pattern)].map((match) => match[1]));

const tokensCss = fs.readFileSync(TOKENS, "utf8");
const defined = namesMatching(tokensCss, DEFINITION);
const sheets = fs
  .readdirSync(STYLES)
  .filter((name) => name.endsWith(".css"))
  .sort();

describe("stylesheet token references", () => {
  it("finds the token file and the sheets it serves", () => {
    // A guard on the guard: a moved directory would otherwise make this suite
    // pass by checking nothing at all.
    expect(defined.size).toBeGreaterThan(50);
    expect(sheets.length).toBeGreaterThan(10);
  });

  it.each(sheets)("%s reads only tokens that exist", (name) => {
    const css = fs.readFileSync(path.join(STYLES, name), "utf8");
    // A sheet may define its own locals (a `--wb-foo` scoped to one component)
    // and read them back; those are as real as a global token.
    const local = namesMatching(css, DEFINITION);
    const undefinedRefs = [...namesMatching(css, REFERENCE)].filter(
      (token) => !defined.has(token) && !local.has(token) && !SET_FROM_TSX.has(token),
    );
    expect(undefinedRefs).toEqual([]);
  });

  it("the search match highlight is painted with a real token", () => {
    // The specific regression: `.wb-search-mark` must resolve to a visible
    // background, not to nothing.
    const css = withoutComments(fs.readFileSync(path.join(STYLES, "search.css"), "utf8"));
    const rule = /\.wb-search-mark\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    const background = /background\s*:\s*var\(\s*(--[a-zA-Z0-9-]+)/.exec(rule)?.[1];
    expect(background).toBeDefined();
    expect(defined.has(background as string)).toBe(true);
  });

  it("that token is defined in both themes, not only the dark default", () => {
    // `--accent-muted` is re-stated in the light block; a token defined once in
    // `:root` and forgotten in the light override is the same bug wearing a
    // different theme.
    const occurrences = [...withoutComments(tokensCss).matchAll(/--accent-muted\s*:/g)];
    expect(occurrences.length).toBeGreaterThanOrEqual(2);
  });
});

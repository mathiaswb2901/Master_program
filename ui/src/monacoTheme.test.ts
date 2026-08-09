/**
 * The editor's palette is `tokens.css`, or it is a second palette.
 *
 * Monaco's `defineTheme` takes concrete colour strings, and the temptation it
 * creates is a hand-written hex table beside the design tokens — one that is
 * correct on the day it is written and silently wrong the first time a token
 * moves, in the one surface a stylesheet-level check cannot see. So this suite
 * asserts the *mechanism* rather than the values: feed the module two different
 * sets of computed tokens and every colour it produces has to move with them.
 * A constant anywhere in the theme fails, whatever it is a constant of.
 *
 * The disk-backed half — that the token names really exist in `tokens.css`, and
 * that the Monaco colour ids really exist in Monaco — is `e2e/editorTheme.test.ts`,
 * which is outside `tsc -b`'s program and may therefore read files.
 *
 * Importing `./editorTheme` costs this suite nothing: the module names
 * `monaco-editor` only in an `import type`, which is exactly what lets the
 * palette live on the editor's chunk and still be asserted here.
 *
 * `document` and `getComputedStyle` are stubbed rather than jsdom'd, as
 * `monacoLoad.test.ts` does: the module reads exactly one property off one
 * element, and 3.3 MB of editor and a DOM stack buy this assertion nothing.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { workbenchThemeData } from "./editorTheme";
import { monacoThemeName } from "./monaco";

/**
 * Two token sets that share no value, so "this colour came from the tokens" is
 * decidable by comparing two builds. Not the real palette: a test that hard-codes
 * `#FBBF24` would be the very drift it exists to catch.
 */
const PALETTE_A = (name: string): string => `#${hash(name).toString(16).padStart(6, "0")}`;
const PALETTE_B = (name: string): string => `#${(0xff_ff_ff - hash(name)).toString(16).padStart(6, "0")}`;

/** Any stable name → colour map; the numbers mean nothing beyond being distinct. */
function hash(name: string): number {
  let value = 0;
  for (const character of name) value = (value * 31 + character.charCodeAt(0)) % 0xff_ff_ff;
  // Never 0x000000 or 0xFFFFFF: both appear in `PALETTE_B` as the other's image
  // and a collision would make a "these differ" assertion vacuous.
  return Math.min(Math.max(value, 1), 0xff_ff_fe);
}

let palette: (name: string) => string = PALETTE_A;

beforeEach(() => {
  vi.stubGlobal("document", { documentElement: {} });
  vi.stubGlobal("getComputedStyle", () => ({
    getPropertyValue: (name: string) => palette(name),
  }));
  palette = PALETTE_A;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the workbench Monaco theme", () => {
  it("inherits nothing, so no built-in colour can survive in it", () => {
    // The substance of the theme. With `inherit: true` every rule the theme does
    // not happen to name keeps VS Code's colour — `variable` at #001188,
    // `metatag` at #e00000 — and the buffer reads as VS Code with our background.
    for (const theme of ["dark", "light"] as const) {
      expect(workbenchThemeData(theme).inherit).toBe(false);
    }
  });

  it("bases each theme on the built-in of the same polarity", () => {
    expect(workbenchThemeData("dark").base).toBe("vs-dark");
    expect(workbenchThemeData("light").base).toBe("vs");
    expect(monacoThemeName("dark")).toBe("workbench-dark");
    expect(monacoThemeName("light")).toBe("workbench-light");
  });

  it("gives every syntax rule a colour Monaco's parser accepts", () => {
    // Monaco matches a rule colour with /^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$/
    // and *throws* on anything else, so an rgba() token reaching a rule takes
    // the whole editor down at theme-definition time rather than looking wrong.
    for (const rule of workbenchThemeData("dark").rules) {
      expect(rule.foreground, rule.token).toMatch(/^[0-9A-Fa-f]{6}$/);
    }
  });

  it("gives every colour slot a hex Monaco can parse, with or without alpha", () => {
    for (const [id, value] of Object.entries(workbenchThemeData("dark").colors)) {
      expect(value, id).toMatch(/^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$/);
    }
  });

  it("reads every colour from the tokens — not one is written down here", () => {
    const a = workbenchThemeData("dark");
    palette = PALETTE_B;
    const b = workbenchThemeData("dark");

    const fixedRules = a.rules
      .filter((rule, index) => rule.foreground === b.rules[index].foreground)
      .map((rule) => rule.token);
    expect(fixedRules, "a syntax colour that does not follow the tokens").toEqual([]);

    const fixedColors = Object.keys(a.colors).filter((id) => a.colors[id] === b.colors[id]);
    expect(fixedColors, "an editor colour that does not follow the tokens").toEqual([]);
  });

  it("carries the alpha it derives, and derives it from a token", () => {
    // The one place a colour is not a whole token: a slider wash, and the slots
    // whose vendor default has to be suppressed. Both are the token's own value
    // with an alpha byte appended, which is what keeps them out of a second
    // palette.
    const { colors } = workbenchThemeData("dark");
    const strong = PALETTE_A("--border-strong").toUpperCase();
    expect(colors["scrollbarSlider.background"].slice(0, 7).toUpperCase()).toBe(strong);
    expect(colors["scrollbarSlider.background"]).toHaveLength(9);
    // 0.53 * 255 = 135 = 0x87.
    expect(colors["scrollbarSlider.background"].slice(7).toUpperCase()).toBe("87");
    // Fully transparent, from a token rather than from the string "#00000000".
    expect(colors["widget.shadow"].slice(7)).toBe("00");
  });

  it("asks Monaco for no weight, because its only weight is 700", () => {
    // DESIGN.md §3: "Weights: 400 body, 500 emphasis/labels, 600 headings/active
    // states. Never 700+." A rule's `fontStyle: "bold"` renders as
    // `font-weight: 700` on every matched span, and Monaco has no vocabulary for
    // 600 — so markdown's `**strong**` was the one place in the window painting
    // a weight the ladder forbids. `SyntaxRule` no longer types `"bold"` at all;
    // this checks the object that is actually handed to `defineTheme`, which a
    // cast could still reach.
    for (const theme of ["dark", "light"] as const) {
      for (const rule of workbenchThemeData(theme).rules) {
        expect(rule.fontStyle ?? "", `${theme}: \`${rule.token}\``).not.toMatch(/bold/);
      }
    }
    // And the italic that is allowed is still there, so this cannot pass by the
    // styles having been dropped wholesale.
    const styled = workbenchThemeData("dark").rules.filter(
      (rule) => rule.fontStyle === "italic",
    );
    expect(styled.map((rule) => rule.token)).toContain("emphasis");
    expect(styled.map((rule) => rule.token)).toContain("comment");
  });

  it("colours the roles the shipped languages actually emit", () => {
    // `monacoBundle.ts` ships 21 grammars plus the JSON service. These are the
    // token types they produce that a reader distinguishes at a glance; a rule
    // dropped from the table here is a role that silently falls back to plain
    // foreground, which looks like the highlighter is broken rather than like a
    // theme decision.
    const tokens = new Set(workbenchThemeData("dark").rules.map((rule) => rule.token));
    for (const role of [
      "comment",
      "string",
      "string.escape",
      "string.key",
      "number",
      "regexp",
      "keyword",
      "operator",
      "delimiter",
      "type",
      "namespace",
      "predefined",
      "function",
      "identifier",
      "variable",
      "annotation",
      "key",
      "tag",
      "metatag",
      "attribute.name",
      "attribute.value",
      "invalid",
    ]) {
      expect(tokens.has(role), `no rule for \`${role}\``).toBe(true);
    }
  });

  it("paints the buffer as a well and the widgets as things that float", () => {
    // ANVIL's ramp, stated as identities rather than as values (DESIGN.md §2.1):
    // the buffer and its gutter are one surface, and everything floating is the
    // overlay surface. Both are what a token swap would break silently.
    const { colors } = workbenchThemeData("dark");
    const code = PALETTE_A("--surface-code");
    expect(colors["editor.background"]).toBe(code);
    expect(colors["editorGutter.background"]).toBe(code);
    expect(colors["minimap.background"]).toBe(code);

    const overlay = PALETTE_A("--surface-overlay");
    for (const id of [
      "editorWidget.background",
      "editorHoverWidget.background",
      "editorSuggestWidget.background",
      "menu.background",
      "quickInput.background",
    ]) {
      expect(colors[id], id).toBe(overlay);
    }

    // Sticky scroll is the one piece of chrome *inside* the buffer, so it steps
    // one notch back toward the frame rather than sitting at buffer value.
    expect(colors["editorStickyScroll.background"]).toBe(PALETTE_A("--surface-panel"));
  });

  it("spends the amber only where the amber is allowed", () => {
    // DESIGN.md §2.4: *where I am*, and *changing right now*. The caret, the
    // find hit under the cursor, the focus ring, a sash being dragged, a bar
    // filling. Nothing standing, and — the specific regression — no gold
    // bracket, whose vendor default is ΔE 10 from `--accent`.
    const { colors } = workbenchThemeData("dark");
    const accent = PALETTE_A("--accent");
    expect(colors["editorCursor.foreground"]).toBe(accent);
    expect(colors["editor.findMatchBorder"]).toBe(accent);
    expect(colors["progressBar.background"]).toBe(accent);
    expect(colors["sash.hoverBorder"]).toBe(accent);
    expect(colors.focusBorder).toBe(PALETTE_A("--focus-ring"));

    const brackets = [1, 2, 3, 4, 5, 6].map(
      (n) => colors[`editorBracketHighlight.foreground${String(n)}`],
    );
    expect(brackets).not.toContain(accent);
    // And not the ANSI yellow either: §2.7 pushed that slot to hue 57° purely to
    // keep it clear of the amber, and spending it on every brace walks it back.
    expect(brackets).not.toContain(PALETTE_A("--ansi-yellow"));
    expect(new Set(brackets).size, "six pairs, six colours").toBe(6);
  });

  it("draws the syntax palette from the terminal's ANSI slots", () => {
    // DESIGN.md §2.7's last line, made checkable: the same green in the buffer
    // and in the shell beside it is what makes the two read as one system.
    const rules = new Map(
      workbenchThemeData("dark").rules.map((rule) => [rule.token, `#${rule.foreground}`]),
    );
    expect(rules.get("string")).toBe(PALETTE_A("--ansi-green"));
    expect(rules.get("comment")).toBe(PALETTE_A("--ansi-bright-black"));
    expect(rules.get("keyword")).toBe(PALETTE_A("--ansi-magenta"));
    expect(rules.get("number")).toBe(PALETTE_A("--ansi-yellow"));
  });

  it("lets the two themes disagree, which is the whole point of rebuilding", () => {
    // `defineWorkbenchTheme` is called again after `data-theme` flips, so the
    // same function has to produce different colours from different tokens —
    // and the name it is registered under has to move with them.
    const dark = workbenchThemeData("dark");
    palette = PALETTE_B;
    const light = workbenchThemeData("light");
    expect(light.colors["editor.background"]).not.toBe(dark.colors["editor.background"]);
    expect(monacoThemeName("light")).not.toBe(monacoThemeName("dark"));
  });
});

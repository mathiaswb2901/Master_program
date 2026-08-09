/**
 * The ANVIL editor theme — every colour Monaco paints, derived from the tokens.
 *
 * **This module is not on the launch path, and that is the reason it is a
 * module.** It is reached only from `monacoBundle.ts`, which is itself behind
 * the single dynamic `import()` in `monaco.ts`, so the tables below are
 * downloaded and parsed *with* the editor rather than before the first pixel.
 * Nothing `main.tsx` can reach statically may import it (`CLAUDE.md`, the launch
 * path; `ui/e2e/perf/bundle.spec.ts` is what fails if that changes). `monaco.ts`
 * takes the builder off the bundle when the chunk lands — a captured function
 * reference rather than an import — which is also what keeps a theme toggle
 * synchronous once the editor exists.
 *
 * `import type * as Monaco` is the only mention of `monaco-editor` here and the
 * compiler erases it. Everything below is strings and lookups, which is what
 * lets `ui/src/monacoTheme.test.ts` assert the whole palette in milliseconds on
 * the node test environment, with no browser and no 3.3 MB import.
 */

import type * as Monaco from "monaco-editor";

import { cssVar, hexVar, type Theme } from "./theme";

/** Fallback for the mono stack, used only when `--font-mono` is unreadable. */
const MONO_FALLBACK =
  "'JetBrains Mono Variable', 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";

/* ---- the ANVIL editor theme -------------------------------------------------
   Monaco's `defineTheme` wants concrete colour strings; this repo forbids a
   colour written anywhere but `tokens.css`. Both hold at once because nothing
   below is a colour — every entry is a **token name**, and its value is read out
   of the live computed tokens at the instant the theme is built (`hexVar` →
   `getComputedStyle`). So there is one palette rather than two, and a theme that
   has drifted from `tokens.css` is impossible rather than merely discouraged.

   `ui/e2e/editorTheme.test.ts` reads this file and fails on a hex literal, on a
   token `tokens.css` does not define, and on a Monaco colour id Monaco does not
   register — the last one because a misspelt id is accepted in silence and then
   simply never paints. */

/** A colour the theme paints with: a token, or a token at a stated alpha. */
type Paint = string | readonly [token: string, alpha: number];

/**
 * Resolve a {@link Paint} against the tokens on screen right now.
 *
 * The alpha form exists for the handful of Monaco *colour* slots that need a
 * wash `tokens.css` has no name for — a scrollbar slider, a minimap slider, and
 * the three slots whose vendor default has to be suppressed outright (a shadow
 * where DESIGN.md §1.4 wants a hairline, a border where the fill already says
 * it). Deriving those from a token is the difference between one palette and
 * two; inventing `#8C8C8C88` here would be the second palette wearing a
 * disguise.
 *
 * Token **rules** never take this path: Monaco parses a rule's colour with a
 * six-digit regular expression and throws on anything else, so a syntax colour
 * is a whole token and nothing else (see {@link SYNTAX}).
 */
function paint(value: Paint): string {
  if (typeof value === "string") return hexVar(value);
  const [token, alpha] = value;
  const byte = Math.round(Math.min(Math.max(alpha, 0), 1) * 255);
  return hexVar(token).slice(0, 7) + byte.toString(16).padStart(2, "0").toUpperCase();
}

interface SyntaxRule {
  /** Monaco token type. Matching is by dotted prefix, so `string` also colours
   * `string.escape` unless a longer rule claims it. */
  token: string;
  /** The design token its foreground comes from. */
  from: string;
  fontStyle?: "italic" | "bold";
}

/**
 * The syntax palette — DESIGN.md §2.7's last line, taken literally: *feed these
 * to the Monaco theme's token colors so terminal, editor and chrome read as one
 * system*. Every colour here is an ANSI slot, so a Python string in the buffer
 * and a Python string echoed by the shell beside it are the same green, in both
 * themes, and the three ANSI rules (≥ 4.5:1 on `--surface-code`, pure greys in
 * the neutral slots, nothing within ΔE 25 of the amber) are inherited whole.
 *
 * Ordered by what the eye should do, not alphabetically: what recedes first,
 * then literals, then structure, then markup, then what is wrong.
 */
const SYNTAX: readonly SyntaxRule[] = [
  // Recedes. Comments and punctuation are the two things a buffer has most of
  // and needs least; giving them the quiet greys is what leaves the rest room.
  { token: "comment", from: "--ansi-bright-black", fontStyle: "italic" },
  { token: "comment.doc", from: "--ansi-bright-black", fontStyle: "italic" },
  { token: "delimiter", from: "--ansi-white" },
  { token: "delimiter.bracket", from: "--ansi-white" },
  { token: "delimiter.html", from: "--ansi-white" },
  { token: "delimiter.xml", from: "--ansi-white" },

  // Literals.
  { token: "string", from: "--ansi-green" },
  { token: "string.escape", from: "--ansi-bright-green" },
  { token: "string.invalid", from: "--ansi-bright-red" },
  { token: "string.link", from: "--ansi-blue" },
  { token: "string.key", from: "--ansi-blue" }, // JSON's object keys
  { token: "string.value", from: "--ansi-green" },
  { token: "number", from: "--ansi-yellow" },
  { token: "constant", from: "--ansi-yellow" },
  { token: "regexp", from: "--ansi-bright-cyan" },

  // Structure.
  { token: "keyword", from: "--ansi-magenta" },
  { token: "operator", from: "--ansi-cyan" },
  { token: "type", from: "--ansi-cyan" },
  { token: "type.identifier", from: "--ansi-cyan" },
  { token: "namespace", from: "--ansi-cyan" },
  { token: "predefined", from: "--ansi-cyan" },
  { token: "function", from: "--ansi-blue" },
  { token: "identifier", from: "--text-primary" },
  { token: "variable", from: "--ansi-bright-blue" },
  { token: "variable.predefined", from: "--ansi-bright-blue" },
  { token: "annotation", from: "--ansi-bright-yellow" },
  { token: "key", from: "--ansi-blue" }, // an ini key; `keyword` is a sibling

  // Markup.
  { token: "tag", from: "--ansi-red" },
  { token: "metatag", from: "--ansi-bright-magenta" },
  { token: "metatag.content", from: "--text-primary" },
  { token: "attribute.name", from: "--ansi-blue" },
  { token: "attribute.value", from: "--ansi-green" },
  // Markdown's own emphasis. `bold` is what `inherit: true` used to supply from
  // the built-in theme, so this is the shipped behaviour restated rather than a
  // new weight: DESIGN.md §3's ladder governs the chrome's typography, and this
  // is the author's `**` rendered in the buffer.
  { token: "emphasis", from: "--text-primary", fontStyle: "italic" },
  { token: "strong", from: "--text-primary", fontStyle: "bold" },

  // Wrong.
  { token: "invalid", from: "--ansi-red" },
];

/**
 * The editor's chrome, colour id by colour id.
 *
 * Larger than it looks like it needs to be, and deliberately: with
 * `inherit: false` (see {@link workbenchThemeData}) anything left out falls
 * back to VS Code's *registry* default rather than to something of ours, so a
 * short list is not a restrained theme — it is a theme that is mostly VS Code
 * in the places a user only sees once they press Ctrl+F.
 */
const COLORS: Readonly<Record<string, Paint>> = {
  // ---- the buffer, and the well it is sunk into (DESIGN.md §2.1, §2.8) -----
  // The buffer is content, not panel chrome: `--surface-code` is one full step
  // below `--surface-panel`, so the code sits *in* the panel rather than level
  // with the tree beside it. The gutter is the same surface — a gutter one step
  // off reads as a second panel glued to the left of the first.
  "editor.background": "--surface-code",
  "editor.foreground": "--text-primary",
  "editorGutter.background": "--surface-code",
  "editorGutter.foldingControlForeground": "--text-tertiary",
  "editorLineNumber.foreground": "--text-tertiary",
  // The caret already spends the amber on *where I am* (§2.4); a second amber
  // mark for the same fact is redundancy, not information. So the active line
  // number is simply the brightest text.
  "editorLineNumber.activeForeground": "--text-primary",
  "editorLineNumber.dimmedForeground": "--text-disabled",
  "editor.lineHighlightBackground": "--surface-hover",
  "editor.lineHighlightBorder": ["--surface-code", 0],
  "editorCursor.foreground": "--accent",
  "editorCursor.background": "--surface-code",
  "editorWhitespace.foreground": "--text-disabled",
  "editorRuler.foreground": "--border-subtle",
  "editorIndentGuide.background1": "--border-subtle",
  "editorIndentGuide.activeBackground1": "--border-strong",
  // Links were demoted off the amber when ANVIL landed (§2.4): navigation is
  // not motion, and Monaco underlines them itself.
  "editorLink.activeForeground": "--text-primary",

  // ---- selection, and the marks that track the caret ----------------------
  "editor.selectionBackground": "--surface-selected",
  "editor.inactiveSelectionBackground": "--surface-hover",
  // Other occurrences of the selected word are a *standing* fact — true for as
  // long as the selection is, and still true when you look away — so they are
  // the neutral wash, never the amber (§2.4).
  "editor.selectionHighlightBackground": "--surface-active",
  "editor.selectionHighlightBorder": ["--surface-code", 0],
  "editor.wordHighlightBackground": "--surface-active",
  "editor.wordHighlightStrongBackground": "--surface-active",
  "editor.wordHighlightBorder": ["--surface-code", 0],
  "editor.wordHighlightStrongBorder": ["--surface-code", 0],
  "editor.rangeHighlightBackground": "--surface-hover",
  "editor.hoverHighlightBackground": "--surface-hover",
  "editorBracketMatch.background": "--surface-active",
  "editorBracketMatch.border": "--border-strong",

  // ---- find: the same language the content-search panel speaks -------------
  // `.wb-search-mark` paints a hit `--accent-muted` on `--text-primary`, so the
  // editor's hits are the same wash — and the *current* one is separated by a
  // hairline rather than by a louder fill (§1.4).
  "editor.findMatchBackground": "--accent-muted",
  "editor.findMatchForeground": "--text-primary",
  "editor.findMatchBorder": "--accent",
  "editor.findMatchHighlightBackground": "--accent-muted",
  "editor.findMatchHighlightForeground": "--text-primary",
  "editor.findMatchHighlightBorder": ["--surface-code", 0],
  "editor.findRangeHighlightBackground": "--surface-hover",

  // ---- bracket pair colorization ------------------------------------------
  // On by default in Monaco, and unset its dark default opens with #FFD700 — a
  // gold ΔE 10 from `--accent`, i.e. indistinguishable from the one colour that
  // is supposed to mean *here* and *now*, on every opening brace in the file.
  // That is the single loudest way the buffer read as a stock drop-in, and
  // `ui/e2e/editorTheme.spec.ts` reads these slots out of the running editor for
  // exactly that reason. Re-cut from the ANSI slots the syntax palette already
  // uses, and the yellow is left out on purpose: it is the slot §2.7 had to push
  // to hue 57° to clear the amber, and spending it here would walk it back.
  "editorBracketHighlight.foreground1": "--ansi-white",
  "editorBracketHighlight.foreground2": "--ansi-blue",
  "editorBracketHighlight.foreground3": "--ansi-magenta",
  "editorBracketHighlight.foreground4": "--ansi-cyan",
  "editorBracketHighlight.foreground5": "--ansi-green",
  "editorBracketHighlight.foreground6": "--ansi-bright-blue",
  "editorBracketHighlight.unexpectedBracket.foreground": "--error",

  // ---- sticky scroll: chrome, so it steps *up* the ramp -------------------
  // The one place inside the buffer where something frames the content instead
  // of being it, so §1.1 applies: one notch lighter in dark, darker in light,
  // and a hairline under it rather than the vendor's drop shadow.
  "editorStickyScroll.background": "--surface-panel",
  "editorStickyScrollHover.background": "--surface-elevated",
  "editorStickyScroll.border": "--border-subtle",
  "editorStickyScroll.shadow": ["--surface-code", 0],

  // ---- everything that floats (§2.1: `--surface-overlay`) ------------------
  "editorWidget.background": "--surface-overlay",
  "editorWidget.foreground": "--text-primary",
  "editorWidget.border": "--border-default",
  "editorWidget.resizeBorder": "--border-strong",
  "editorHoverWidget.background": "--surface-overlay",
  "editorHoverWidget.foreground": "--text-primary",
  "editorHoverWidget.border": "--border-default",
  "editorHoverWidget.statusBarBackground": "--surface-elevated",
  "editorHoverWidget.highlightForeground": "--accent",
  "editorSuggestWidget.background": "--surface-overlay",
  "editorSuggestWidget.foreground": "--text-secondary",
  "editorSuggestWidget.border": "--border-default",
  "editorSuggestWidget.selectedBackground": "--surface-selected",
  "editorSuggestWidget.selectedForeground": "--text-primary",
  "editorSuggestWidget.highlightForeground": "--accent",
  "editorSuggestWidget.focusHighlightForeground": "--accent",
  "editorSuggestWidgetStatus.foreground": "--text-tertiary",
  "widget.border": "--border-default",
  // Suppressed rather than tinted: §4 puts elevation on the three `--shadow-*`
  // tokens, and `styles/editor.css` spends them on these widgets. A vendor
  // shadow underneath would be a fourth level nobody chose.
  "widget.shadow": ["--surface-code", 0],
  "menu.background": "--surface-overlay",
  "menu.foreground": "--text-secondary",
  "menu.border": "--border-default",
  "menu.selectionBackground": "--surface-selected",
  "menu.selectionForeground": "--text-primary",
  "menu.separatorBackground": "--border-subtle",
  "quickInput.background": "--surface-overlay",
  "quickInput.foreground": "--text-primary",
  "quickInputTitle.background": "--surface-elevated",
  "quickInputList.focusBackground": "--surface-selected",
  "quickInputList.focusForeground": "--text-primary",
  "pickerGroup.foreground": "--text-tertiary",
  "pickerGroup.border": "--border-subtle",

  // ---- list rows (the suggest list, the standalone quick-access widgets) ---
  // Same anatomy as the QuickBar's own rows (§6.5): the selected row is an
  // amber wash carrying `--text-primary`, never amber text.
  "list.hoverBackground": "--surface-hover",
  "list.hoverForeground": "--text-primary",
  "list.focusBackground": "--surface-selected",
  "list.focusForeground": "--text-primary",
  "list.focusOutline": "--focus-ring",
  "list.activeSelectionBackground": "--surface-selected",
  "list.activeSelectionForeground": "--text-primary",
  "list.inactiveSelectionBackground": "--surface-active",
  "list.inactiveSelectionForeground": "--text-secondary",
  "list.highlightForeground": "--accent",
  "list.focusHighlightForeground": "--accent",

  // ---- inputs (the find widget's two fields, and its validation) ----------
  "input.background": "--surface-elevated",
  "input.foreground": "--text-primary",
  "input.border": "--border-default",
  "input.placeholderForeground": "--text-tertiary",
  "inputOption.activeBorder": "--accent",
  "inputOption.activeBackground": "--accent-muted",
  "inputOption.activeForeground": "--text-primary",
  "inputOption.hoverBackground": "--surface-hover",
  "inputValidation.errorBackground": "--error-bg",
  "inputValidation.errorBorder": "--error",
  "inputValidation.errorForeground": "--text-primary",
  "inputValidation.warningBackground": "--warn-bg",
  "inputValidation.warningBorder": "--warn",
  "inputValidation.warningForeground": "--text-primary",
  "inputValidation.infoBackground": "--info-bg",
  "inputValidation.infoBorder": "--info",
  "inputValidation.infoForeground": "--text-primary",

  // ---- scrollbar, overview ruler, minimap ---------------------------------
  // The minimap is off (`panels/CodeEditor.tsx` — density, §1.5), and is themed
  // anyway: the option is one line, and a minimap switched on to find something
  // must not be the one surface in the window still wearing VS Code's colours.
  "scrollbarSlider.background": ["--border-strong", 0.53],
  "scrollbarSlider.hoverBackground": ["--border-strong", 0.73],
  "scrollbarSlider.activeBackground": "--border-strong",
  "scrollbar.shadow": ["--border-subtle", 0.5],
  "editorOverviewRuler.background": "--surface-code",
  "editorOverviewRuler.border": "--surface-code",
  "editorOverviewRuler.findMatchForeground": "--accent",
  "editorOverviewRuler.selectionHighlightForeground": "--border-strong",
  "editorOverviewRuler.rangeHighlightForeground": "--border-subtle",
  "editorOverviewRuler.wordHighlightForeground": "--border-strong",
  "editorOverviewRuler.wordHighlightStrongForeground": "--border-strong",
  "editorOverviewRuler.errorForeground": "--error",
  "editorOverviewRuler.warningForeground": "--warn",
  "editorOverviewRuler.infoForeground": "--info",
  "minimap.background": "--surface-code",
  "minimap.findMatchHighlight": "--accent",
  "minimap.selectionHighlight": "--border-strong",
  "minimap.errorHighlight": "--error",
  "minimap.warningHighlight": "--warn",
  "minimap.infoHighlight": "--info",
  "minimapSlider.background": ["--border-strong", 0.24],
  "minimapSlider.hoverBackground": ["--border-strong", 0.35],
  "minimapSlider.activeBackground": ["--border-strong", 0.5],

  // ---- diagnostics (§2.5) --------------------------------------------------
  "editorError.foreground": "--error",
  "editorWarning.foreground": "--warn",
  "editorInfo.foreground": "--info",
  "editorHint.foreground": "--text-tertiary",

  // ---- the rest of the chrome Monaco is allowed to paint ------------------
  focusBorder: "--focus-ring",
  errorForeground: "--error",
  descriptionForeground: "--text-tertiary",
  "icon.foreground": "--text-tertiary",
  "toolbar.hoverBackground": "--surface-hover",
  "toolbar.activeBackground": "--surface-active",
  // A progress bar is the app "changing right now" — one of the amber's two
  // permitted meanings, and the rarer one (§2.4).
  "progressBar.background": "--accent",
  "textLink.foreground": "--text-primary",
  "textLink.activeForeground": "--text-primary",
  "selection.background": "--surface-selected",
  "sash.hoverBorder": "--accent",
};

/**
 * The theme Monaco is handed, built from the tokens on screen at the moment of
 * the call. Exported so it can be asserted without a browser and without 3.3 MB
 * of editor (`ui/src/monacoTheme.test.ts`).
 *
 * **`inherit: false`, and that is the substance of this theme.** Inheriting
 * keeps every built-in rule the theme does not happen to name, and the built-in
 * list is long and loud: `variable` at #001188, `metatag` at #e00000,
 * `tag.id.pug`, `meta.scss`. A theme that overrides fourteen of them is a VS
 * Code theme with our background colour — which is exactly how the editor read
 * before this. Turning inheritance off makes {@link SYNTAX} the whole palette:
 * a token nothing here names falls through to `editor.foreground`, which is
 * `--text-primary`, which is right.
 */
export function workbenchThemeData(theme: Theme): Monaco.editor.IStandaloneThemeData {
  return {
    base: theme === "dark" ? "vs-dark" : "vs",
    inherit: false,
    rules: SYNTAX.map(({ token, from, fontStyle }) => ({
      token,
      // Monaco's rule parser takes six hex digits with or without the `#`, and
      // throws on anything else — including an alpha channel.
      foreground: hexVar(from).slice(1),
      ...(fontStyle === undefined ? {} : { fontStyle }),
    })),
    colors: Object.fromEntries(
      Object.entries(COLORS).map(([id, value]) => [id, paint(value)]),
    ),
  };
}

/**
 * The buffer's type, read from `--type-code` rather than restated as two
 * numbers beside it.
 *
 * DESIGN.md §3 names `--type-code` "Monaco default", and until now the editor
 * carried 13/20 as literals — the same drift the colours were carrying, in the
 * one dimension a palette test would never look at. Monaco wants numbers, so
 * the token's `px` is stripped; each falls back to what the literal was if a
 * token is ever unreadable, which is the only state in which this can be asked
 * before the stylesheet applies.
 *
 * The mono stack falls back to a literal for the same reason, and it is the one
 * string in this file that repeats a token value — a font list is not a colour,
 * `tokens.css` is still where it is edited, and the alternative to a fallback is
 * an editor with no monospace font at all.
 */
export function editorFontOptions(): {
  fontFamily: string;
  fontSize: number;
  lineHeight: number;
} {
  const px = (name: string, fallback: number): number => {
    const parsed = Number.parseFloat(cssVar(name));
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  };
  return {
    fontFamily: cssVar("--font-mono") || MONO_FALLBACK,
    fontSize: px("--type-code-size", 13),
    lineHeight: px("--type-code-line", 20),
  };
}

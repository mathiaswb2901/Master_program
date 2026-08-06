/**
 * Monaco, kept off the launch path.
 *
 * This module is what the rest of the app imports, and it contains **no static
 * reference to `monaco-editor`** — only `import type`, which the compiler
 * erases. The bundle itself (`./monacoBundle`) is reached through one dynamic
 * `import()`, so rollup gives it its own chunk and the entry chunk a cold start
 * must parse before it can paint no longer carries an editor.
 *
 * That is the whole design, and everything below follows from it:
 *
 * * **The pure half is always available.** `MONO_FONT`, `languageForPath`,
 *   `editorPathProp` and `monacoThemeName` are strings and lookups. The
 *   Terminal reads the font from here, the store asks for a theme name — none
 *   of that should cost 3.5 MB.
 * * **The model helpers no-op until the editor exists.** `setModelContent` and
 *   `disposeModel` act on models, and until Monaco has loaded there are none,
 *   so "not loaded" and "no model for that path" are the same answer — which
 *   the store already handles (`setModelContent(...) ?? content`).
 * * **Loading is idempotent and shared.** `loadMonaco` memoizes its promise, so
 *   the idle-time prefetch and a user clicking a file in the same moment
 *   produce one download, not two.
 */

import type * as Monaco from "monaco-editor";

import { cssVar, documentTheme, hexVar, toHex, type Theme } from "./theme";

export const MONO_FONT =
  "'JetBrains Mono Variable', 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";

/** The loaded API, or null while nothing has needed an editor yet. */
let api: typeof Monaco | null = null;
/** In-flight (or settled) load, so concurrent callers share one import. */
let loading: Promise<typeof Monaco> | null = null;

/**
 * Load Monaco, configure it, and define the theme. Safe to call from anywhere,
 * any number of times; the work happens once.
 *
 * The theme is defined *here* rather than by the caller because it must exist
 * before the first `<Editor>` renders — `@monaco-editor/react` applies the
 * theme name on creation, and for a name it does not know Monaco silently
 * falls back to a built-in one.
 *
 * **This function takes no theme, and that is the point.** It used to, and the
 * value was captured when the load was *scheduled* — which for the idle
 * prefetch is long before it lands. `defineWorkbenchTheme` can do nothing while
 * `api` is null, so a toggle inside that window was dropped, and the theme then
 * defined here carried the stale *name* while reading the *live* tokens: the
 * two disagreed, and the name the editor went on to ask for was never defined
 * at all. Reading `documentTheme()` at the moment the bundle lands is what
 * keeps the name and the colors describing the same theme, and there is no
 * longer a parameter through which they can drift apart
 * (`ui/e2e/theme.spec.ts`, `ui/src/monacoLoad.test.ts`).
 */
export async function loadMonaco(): Promise<typeof Monaco> {
  loading ??= import("./monacoBundle").then((bundle) => {
    api = bundle.configureBundle();
    defineWorkbenchTheme(documentTheme());
    return api;
  });
  return loading;
}

/**
 * Start the load when the browser has nothing better to do.
 *
 * Monaco's first construction is ~100 ms of work, and moving it off the launch
 * path only helps if the user does not simply pay it on their first click
 * instead. So the bytes are warmed after first paint — deliberately with **no
 * `timeout`**: an idle callback that fires anyway is a 3.5 MB parse landing in
 * the middle of whatever the user is doing, which is the problem this change
 * exists to remove, not a smaller version of it. A page that never goes idle
 * simply loads the editor on demand, as it would have anyway.
 */
export function prefetchMonaco(): void {
  if (loading !== null) return;
  const warm = (): void => {
    void loadMonaco();
  };
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number })
    .requestIdleCallback;
  if (idle === undefined) setTimeout(warm, 1_000);
  else idle(warm);
}

export function monacoThemeName(theme: Theme): string {
  return theme === "dark" ? "workbench-dark" : "workbench-light";
}

/**
 * (Re)build the Monaco theme from the CURRENT computed tokens. Call after the
 * data-theme attribute changes so both themes are defined from live values.
 * Chrome colors per DESIGN.md §2.8; syntax colors from the ANSI palette.
 *
 * A no-op before the editor is loaded, and safely so: `loadMonaco` defines
 * whatever theme is current at the moment it finishes, so a toggle that landed
 * inside the load window is picked up there rather than lost here.
 */
export function defineWorkbenchTheme(theme: Theme): void {
  if (api === null) return;
  const rule = (token: string, varName: string): Monaco.editor.ITokenThemeRule => ({
    token,
    foreground: hexVar(varName).slice(1),
  });
  api.editor.defineTheme(monacoThemeName(theme), {
    base: theme === "dark" ? "vs-dark" : "vs",
    inherit: true,
    rules: [
      rule("comment", "--ansi-bright-black"),
      rule("string", "--ansi-green"),
      rule("string.escape", "--ansi-bright-green"),
      rule("number", "--ansi-yellow"),
      rule("keyword", "--ansi-magenta"),
      rule("operator", "--ansi-cyan"),
      rule("type", "--ansi-cyan"),
      rule("type.identifier", "--ansi-cyan"),
      rule("namespace", "--ansi-cyan"),
      rule("function", "--ansi-blue"),
      rule("regexp", "--ansi-cyan"),
      rule("tag", "--ansi-red"),
      rule("attribute.name", "--ansi-blue"),
      rule("attribute.value", "--ansi-green"),
    ],
    colors: {
      "editor.background": toHex(cssVar("--surface-panel")),
      "editor.foreground": toHex(cssVar("--text-primary")),
      "editor.lineHighlightBackground": toHex(cssVar("--surface-hover")),
      "editor.selectionBackground": toHex(cssVar("--surface-selected")),
      "editor.inactiveSelectionBackground": toHex(cssVar("--surface-hover")),
      "editorLineNumber.foreground": toHex(cssVar("--text-tertiary")),
      "editorLineNumber.activeForeground": toHex(cssVar("--text-secondary")),
      "editorCursor.foreground": toHex(cssVar("--accent")),
      "editorGutter.background": toHex(cssVar("--surface-panel")),
      "editorWidget.background": toHex(cssVar("--surface-overlay")),
      "editorWidget.border": toHex(cssVar("--border-default")),
      "editorSuggestWidget.selectedBackground": toHex(cssVar("--surface-selected")),
      "editorIndentGuide.background1": toHex(cssVar("--border-subtle")),
      "scrollbarSlider.background": hexVar("--border-strong") + "88",
      "scrollbarSlider.hoverBackground": hexVar("--border-strong") + "BB",
      "scrollbarSlider.activeBackground": hexVar("--border-strong"),
      "editorOverviewRuler.border": toHex(cssVar("--surface-panel")),
      focusBorder: toHex(cssVar("--focus-ring")),
    },
  });
}

// ---- model helpers ----------------------------------------------------------

/** Uri scheme must match the `path` prop given to <Editor> (see EditorArea). */
export const editorPathProp = (path: string): string => `file:///${path}`;

let activeEditor: Monaco.editor.IStandaloneCodeEditor | null = null;

export function setActiveEditor(editor: Monaco.editor.IStandaloneCodeEditor | null): void {
  activeEditor = editor;
}

/**
 * Withdraw an editor that is going away — and *only* it.
 *
 * There is more than one editor in the window now (the tab strip's, plus one
 * per `editors#<path>` pane), so "an editor unmounted" is not "there is no
 * active editor": closing one pane must not cost the pane beside it the cursor
 * and scroll restore that `setModelContent` does. A no-op unless the editor
 * being dropped is the one currently registered.
 */
export function clearActiveEditor(editor: Monaco.editor.IStandaloneCodeEditor): void {
  if (activeEditor === editor) activeEditor = null;
}

/** The model for a path, or null — including "the editor has not loaded yet",
 * which is indistinguishable from "no model" and means the same thing here. */
function modelFor(path: string): Monaco.editor.ITextModel | null {
  return api?.editor.getModel(api.Uri.parse(editorPathProp(path))) ?? null;
}

/**
 * Replace a model's content from an external (on-disk) change. If the model is
 * currently shown in the editor, cursor and scroll position are preserved.
 * Returns the content as the model now holds it (Monaco normalizes mixed line
 * endings), or null when no model exists for the path yet — callers should
 * store the returned value so dirty-tracking compares like with like.
 */
export function setModelContent(path: string, content: string): string | null {
  const model = modelFor(path);
  if (model === null) return null;
  if (activeEditor && activeEditor.getModel() === model) {
    const viewState = activeEditor.saveViewState();
    model.setValue(content);
    if (viewState) activeEditor.restoreViewState(viewState);
  } else {
    model.setValue(content);
  }
  return model.getValue();
}

/** Drop the cached model when a tab closes so reopening reloads from disk. */
export function disposeModel(path: string): void {
  modelFor(path)?.dispose();
}

/**
 * Extension -> Monaco language id.
 *
 * **This table and `monacoBundle.ts`'s import list are one list.** Every value
 * here must have a contribution imported there, or the file opens as plain
 * text with no warning; every contribution imported there should be reachable
 * from here, or it is bytes on a path nobody can ask for.
 */
const LANG_BY_EXT: Record<string, string> = {
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  json: "json",
  css: "css",
  scss: "scss",
  less: "less",
  html: "html",
  htm: "html",
  md: "markdown",
  yml: "yaml",
  yaml: "yaml",
  toml: "ini",
  ini: "ini",
  sql: "sql",
  sh: "shell",
  bash: "shell",
  ps1: "powershell",
  psm1: "powershell",
  psd1: "powershell",
  bat: "bat",
  cmd: "bat",
  xml: "xml",
  svg: "xml",
  rs: "rust",
  go: "go",
  java: "java",
  c: "c",
  h: "c",
  cpp: "cpp",
  hpp: "cpp",
  cs: "csharp",
};

/** Every language id this app can ask Monaco for — the contract
 * `monacoBundle.ts` has to satisfy, asserted in `monaco.test.ts`. */
export const SHIPPED_LANGUAGES: readonly string[] = [
  ...new Set([...Object.values(LANG_BY_EXT), "dockerfile"]),
];

export function languageForPath(path: string): string {
  const name = (path.split("/").pop() ?? "").toLowerCase();
  if (name === "dockerfile") return "dockerfile";
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1) : "";
  return LANG_BY_EXT[ext] ?? "plaintext";
}

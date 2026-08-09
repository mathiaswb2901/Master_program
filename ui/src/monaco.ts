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
 * * **This module owns model lifetime**, because more than one pane can be
 *   looking at one file and none of them owns what they are looking at. See
 *   the registry below (`acquireModel`/`releaseModel`/`disposeModel`) for the
 *   rule and for why "dispose at zero refs" is not it.
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
      // The buffer is a content well, not panel chrome: `--surface-code` is one
      // full step below `--surface-panel` on ANVIL's ramp (DESIGN.md §2.1), so
      // the code sits *in* the panel instead of level with the tree beside it.
      "editor.background": toHex(cssVar("--surface-code")),
      "editor.foreground": toHex(cssVar("--text-primary")),
      "editor.lineHighlightBackground": toHex(cssVar("--surface-hover")),
      "editor.selectionBackground": toHex(cssVar("--surface-selected")),
      "editor.inactiveSelectionBackground": toHex(cssVar("--surface-hover")),
      "editorLineNumber.foreground": toHex(cssVar("--text-tertiary")),
      "editorLineNumber.activeForeground": toHex(cssVar("--text-secondary")),
      "editorCursor.foreground": toHex(cssVar("--accent")),
      "editorGutter.background": toHex(cssVar("--surface-code")),
      "editorWidget.background": toHex(cssVar("--surface-overlay")),
      "editorWidget.border": toHex(cssVar("--border-default")),
      "editorSuggestWidget.selectedBackground": toHex(cssVar("--surface-selected")),
      "editorIndentGuide.background1": toHex(cssVar("--border-subtle")),
      "scrollbarSlider.background": hexVar("--border-strong") + "88",
      "scrollbarSlider.hoverBackground": hexVar("--border-strong") + "BB",
      "scrollbarSlider.activeBackground": hexVar("--border-strong"),
      "editorOverviewRuler.border": toHex(cssVar("--surface-code")),
      focusBorder: toHex(cssVar("--focus-ring")),
    },
  });
}

// ---- model helpers ----------------------------------------------------------

/**
 * Uri scheme must match the `path` prop given to <Editor> (see EditorArea).
 *
 * Separators are normalised because this string *is* the model's identity:
 * Monaco keys models by Uri, so `src\bid.py` and `src/bid.py` would be two
 * models — two buffers over one file, each able to save over the other. Every
 * path the app has comes from the server as POSIX-relative, so this only ever
 * fires on a path that took a Windows detour, which is exactly the case worth
 * catching.
 */
export const editorPathProp = (path: string): string => `file:///${path.replace(/\\/g, "/")}`;

/**
 * The model registry: **who owns a Monaco text model, and when it dies.**
 *
 * A pane is a view onto a resource it does not own (CLAUDE.md, ROADMAP product
 * principle 4). For the editor the resource is the *model* — the buffer, its
 * undo stack, its markers — and the views are the tab strip's editor plus every
 * `editors#<path>` pane. `@monaco-editor/react` does not know that: its
 * `<Editor>` disposes whatever model it is showing when it unmounts, which is
 * only correct when there is exactly one editor in the window. With two, the
 * first pane to close took the model out from under the other, and Monaco's own
 * reaction is to detach it everywhere — every editor attached to a model
 * registers `model.onWillDispose(() => this.setModel(null))`, and detaching
 * removes the view's DOM node — so the surviving pane went *blank*, tab strip
 * and unsaved dot still on screen above nothing. Hence `keepCurrentModel` on
 * every `<Editor>` (`panels/CodeEditor.tsx`) and this map.
 *
 * **The disposal decision, stated: a model dies with its FILE, never with a
 * pane.** Two things end it, both explicit:
 *
 *  1. the store closes the file (`closeFile`, `adoptWorkspace`) and calls
 *     `disposeModel` — the tab is gone, so the buffer should be too, and
 *     reopening must come back from disk rather than from a stale buffer;
 *  2. that retirement then waits for the last view to let go, so a model is
 *     never disposed under a live editor either — the mirror of the same bug,
 *     which closing a tab while a pane showed the same file used to hit.
 *
 * The rejected alternative is "dispose at zero refs". It is wrong here for one
 * concrete reason: a file open in the tab strip but *displayed* by nobody (the
 * strip is on another tab) has zero views, and disposing there would throw away
 * the undo history and the markers of a file the user still has open, at the
 * moment they close an unrelated pane. "Keep-N for fast reopen" was rejected
 * for the opposite reason — the keep set would be a second, invisible answer to
 * "is this file open?", and reopening is supposed to re-read disk.
 */
interface ModelEntry {
  /** Mounted editors currently displaying this model. */
  views: number;
  /** The store has closed the file: dispose as soon as the last view lets go. */
  retired: boolean;
}
const models = new Map<string, ModelEntry>();

/** Per-*view* scroll and cursor, keyed by pane and path — never by path alone.
 * Two panes on one file scroll independently because where you are looking is
 * the view's state, not the model's; a map keyed by path is the same singleton
 * assumption in miniature (and is what `@monaco-editor/react`'s own
 * `saveViewState` does, which is why this module turns it off). */
const viewStates = new Map<string, Map<string, Monaco.editor.ICodeEditorViewState>>();

export function rememberViewState(
  pane: string,
  path: string,
  state: Monaco.editor.ICodeEditorViewState | null,
): void {
  if (state === null) return;
  const key = editorPathProp(path);
  const byPane = viewStates.get(key) ?? new Map<string, Monaco.editor.ICodeEditorViewState>();
  byPane.set(pane, state);
  viewStates.set(key, byPane);
}

export function recallViewState(
  pane: string,
  path: string,
): Monaco.editor.ICodeEditorViewState | null {
  return viewStates.get(editorPathProp(path))?.get(pane) ?? null;
}

/** A view is now displaying this path. Balanced by `releaseModel`. */
export function acquireModel(path: string): void {
  const key = editorPathProp(path);
  const entry = models.get(key);
  if (entry === undefined) models.set(key, { views: 1, retired: false });
  else {
    entry.views += 1;
    // Somebody wants it again — a pane that reopened a file the strip closed.
    entry.retired = false;
  }
}

/**
 * A view has stopped displaying this path.
 *
 * The model survives unless the store has already retired it: closing a pane
 * closes a *view*, and the file behind it is still open in the tab strip with
 * its undo stack intact.
 */
export function releaseModel(path: string): void {
  const key = editorPathProp(path);
  const entry = models.get(key);
  if (entry === undefined) return;
  entry.views = Math.max(0, entry.views - 1);
  if (entry.views === 0 && entry.retired) destroyModel(key);
}

/** Actually end it, and forget every view's memory of where it was looking. */
function destroyModel(key: string): void {
  models.delete(key);
  // Dispose *before* forgetting the view states, not after: Monaco answers a
  // disposed model by calling `setModel(null)` on every editor still attached
  // to it, and that fires the `onWillChangeModel` handler in `CodeEditor`,
  // which files one last view state under this very key.
  const model = api?.editor.getModel(api.Uri.parse(key)) ?? null;
  model?.dispose();
  viewStates.delete(key);
}

/** The model for a path, or null — including "the editor has not loaded yet",
 * which is indistinguishable from "no model" and means the same thing here. */
function modelFor(path: string): Monaco.editor.ITextModel | null {
  return api?.editor.getModel(api.Uri.parse(editorPathProp(path))) ?? null;
}

/** Every mounted editor currently showing this model — one per pane, and the
 * reason nothing here reaches for "the" editor. */
function viewsOf(model: Monaco.editor.ITextModel): readonly Monaco.editor.ICodeEditor[] {
  return (api?.editor.getEditors() ?? []).filter((editor) => editor.getModel() === model);
}

/**
 * Replace a model's content from an external (on-disk) change, preserving where
 * **every** pane showing it was looking — `setValue` is a full-model edit, so a
 * pane that is not restored jumps to the top of the file.
 *
 * Returns the content as the model now holds it (Monaco normalizes mixed line
 * endings), or null when no model exists for the path yet — callers should
 * store the returned value so dirty-tracking compares like with like.
 */
export function setModelContent(path: string, content: string): string | null {
  const model = modelFor(path);
  if (model === null) return null;
  const views = viewsOf(model);
  const states = views.map((editor) => editor.saveViewState());
  model.setValue(content);
  views.forEach((editor, index) => {
    const state = states[index];
    if (state !== undefined && state !== null) editor.restoreViewState(state);
  });
  return model.getValue();
}

/**
 * The file is closed: retire its model so reopening reloads from disk.
 *
 * Deferred while a view still holds it, which is what stops the *other* half of
 * the plural bug — closing the last tab while an `editors#<path>` pane is still
 * on screen used to dispose the model under that pane's live editor. The pane
 * unmounts a moment later (it renders its "closed / Reopen" note instead), and
 * `releaseModel` finishes the job then.
 */
export function disposeModel(path: string): void {
  const key = editorPathProp(path);
  const entry = models.get(key);
  if (entry !== undefined && entry.views > 0) {
    entry.retired = true;
    return;
  }
  destroyModel(key);
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

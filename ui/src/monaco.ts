/**
 * Monaco setup: bundled monaco (no CDN), Vite workers, workbench themes derived
 * from the design tokens, and model helpers for external (on-disk) updates.
 */

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";

import { cssVar, hexVar, toHex, type Theme } from "./theme";

export const MONO_FONT =
  "'JetBrains Mono Variable', 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace";

export function initMonaco(theme: Theme): void {
  (globalThis as { MonacoEnvironment?: monaco.Environment }).MonacoEnvironment = {
    getWorker(_workerId: string, label: string): Worker {
      switch (label) {
        case "json":
          return new jsonWorker();
        case "css":
        case "scss":
        case "less":
          return new cssWorker();
        case "html":
        case "handlebars":
        case "razor":
          return new htmlWorker();
        case "typescript":
        case "javascript":
          return new tsWorker();
        default:
          return new editorWorker();
      }
    },
  };
  loader.config({ monaco });
  defineWorkbenchTheme(theme);
}

export function monacoThemeName(theme: Theme): string {
  return theme === "dark" ? "workbench-dark" : "workbench-light";
}

/**
 * (Re)build the Monaco theme from the CURRENT computed tokens. Call after the
 * data-theme attribute changes so both themes are defined from live values.
 * Chrome colors per DESIGN.md §2.8; syntax colors from the ANSI palette.
 */
export function defineWorkbenchTheme(theme: Theme): void {
  const rule = (token: string, varName: string): monaco.editor.ITokenThemeRule => ({
    token,
    foreground: hexVar(varName).slice(1),
  });
  monaco.editor.defineTheme(monacoThemeName(theme), {
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

export const uriFor = (path: string): monaco.Uri => monaco.Uri.parse(editorPathProp(path));

let activeEditor: monaco.editor.IStandaloneCodeEditor | null = null;

export function setActiveEditor(editor: monaco.editor.IStandaloneCodeEditor | null): void {
  activeEditor = editor;
}

/**
 * Replace a model's content from an external (on-disk) change. If the model is
 * currently shown in the editor, cursor and scroll position are preserved.
 * Returns the content as the model now holds it (Monaco normalizes mixed line
 * endings), or null when no model exists for the path yet — callers should
 * store the returned value so dirty-tracking compares like with like.
 */
export function setModelContent(path: string, content: string): string | null {
  const model = monaco.editor.getModel(uriFor(path));
  if (!model) return null;
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
  monaco.editor.getModel(uriFor(path))?.dispose();
}

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

export function languageForPath(path: string): string {
  const name = (path.split("/").pop() ?? "").toLowerCase();
  if (name === "dockerfile") return "dockerfile";
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1) : "";
  return LANG_BY_EXT[ext] ?? "plaintext";
}

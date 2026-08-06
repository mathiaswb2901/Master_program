/**
 * Type shims for the Monaco entry points a hand-picked build reaches for.
 *
 * `monaco-editor` ships exactly one declaration file — `esm/vs/editor/
 * editor.api.d.ts`, which its `package.json` points `typings` at — and it
 * describes the *barrel*. The deeper ESM entry points have no `.d.ts` of their
 * own, so importing them (which is the only way to leave the barrel's ~90
 * language contributions behind) needs the two declarations below.
 *
 * The language services under `monaco-editor/esm/vs/language` are deliberately
 * absent: each ships its own `.d.ts` and is typed for real.
 */

/**
 * The editor without any language: `editor.all` (every editor contribution —
 * find, folding, suggest, bracket matching, context menu, sticky scroll) plus
 * the standalone quick-access widgets, re-exporting the same API surface the
 * barrel does. Identical types, one import shallower.
 */
declare module "monaco-editor/esm/vs/editor/edcore.main" {
  export * from "monaco-editor/esm/vs/editor/editor.api";
}

/** One language's Monarch grammar and bracket/comment configuration. Registered
 * for its side effect only, so an untyped shorthand module is the whole truth. */
declare module "monaco-editor/esm/vs/basic-languages/*.contribution";

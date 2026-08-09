/**
 * The Monaco build Workbench ships — the *only* module that imports
 * `monaco-editor`, and one nothing may import statically.
 *
 * It exists because of a measurement. `import * as monaco from "monaco-editor"`
 * pulls the barrel, `main.tsx` pulled that in statically, and the result was an
 * eager entry chunk of 3,999 KiB (1,030 KiB gzipped) of which **88% was
 * Monaco** — downloaded, parsed and evaluated before `DOMContentLoaded`, on
 * every cold start, whether or not the user ever opened a file. Reaching it
 * through `import("./monacoBundle")` (see `monaco.ts`) moves all of it onto the
 * first file open, and an idle-time prefetch usually gets there first: the
 * entry chunk is 191 KiB gzipped, first paint went 618 ms to 160 ms, and
 * opening a file still takes the ~150 ms it always did
 * (`ui/e2e/perf/bundle.spec.ts`, `open-file.spec.ts`).
 *
 * Two things are hand-picked here, and both are stated rather than defaulted:
 *
 * **The editor**: `edcore.main` instead of the barrel. Same editor — it is
 * `editor.all` (find, folding, suggest, bracket matching, multicursor, context
 * menu, sticky scroll, …) plus the standalone quick-access widgets — minus the
 * barrel's five language imports, which is the only difference. Nothing an
 * `IStandaloneCodeEditor` does is given up.
 *
 * **The languages**: the list below is exactly the list `languageForPath`
 * (`monaco.ts`) can return, and that is the rule — a language the extension
 * table cannot name is bytes on a path nobody can ask for, and a language the
 * table names but this file omits is a file that silently opens as plain text.
 * Dropped: the other ~66 basic-language contributions the barrel registers
 * (ABAP, Apex, Bicep, CameLIGO, Cypher, ECL, Lexon, MSDAX, Pascaligo, Postiats,
 * Q#, Redshift, Sophia, SystemVerilog, TypeSpec, WGSL and the rest). None of
 * them is reachable from the extension table, and the alternative — extending
 * the table to 89 languages — buys a Nordic power-market analyst nothing.
 *
 * **The language services are not the same thing as the languages.** Monaco
 * ships four (`vs/language/{json,css,html,typescript}`): a worker per language
 * doing validation, completion and formatting on top of the grammar. Only
 * **JSON** is kept, and it is kept because it is not optional — JSON has no
 * basic-language grammar at all, so its service *is* its syntax highlighting,
 * and it is the format the app's own files (`layouts.json`, `package.json`,
 * `tsconfig.json`) are written in. CSS, HTML and TypeScript are dropped: their
 * grammars are kept above, so highlighting, brackets, auto-closing and comment
 * toggling are unchanged, and what goes with them is a worker each. For
 * TypeScript that is a feature worth losing on its own terms, and it was
 * measured rather than asserted: the standalone service type-checks each file
 * in isolation, with no `tsconfig.json` and no module resolution, so opening
 * this repository's own `ui/src/store.ts` — a file `tsc` compiles clean — drew
 * **10 red squiggles** on the old build and draws **0** on this one, with the
 * same seven token colours either way. Wrong squiggles cost more than none.
 */

import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/edcore.main";

// The languages, in the order `LANG_BY_EXT` names them.
import "monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution";
import "monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution";
import "monaco-editor/esm/vs/basic-languages/css/css.contribution";
import "monaco-editor/esm/vs/basic-languages/scss/scss.contribution";
import "monaco-editor/esm/vs/basic-languages/less/less.contribution";
import "monaco-editor/esm/vs/basic-languages/html/html.contribution";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution";
import "monaco-editor/esm/vs/basic-languages/ini/ini.contribution";
import "monaco-editor/esm/vs/basic-languages/sql/sql.contribution";
import "monaco-editor/esm/vs/basic-languages/shell/shell.contribution";
import "monaco-editor/esm/vs/basic-languages/powershell/powershell.contribution";
import "monaco-editor/esm/vs/basic-languages/bat/bat.contribution";
import "monaco-editor/esm/vs/basic-languages/xml/xml.contribution";
import "monaco-editor/esm/vs/basic-languages/rust/rust.contribution";
import "monaco-editor/esm/vs/basic-languages/go/go.contribution";
import "monaco-editor/esm/vs/basic-languages/java/java.contribution";
import "monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution";
import "monaco-editor/esm/vs/basic-languages/csharp/csharp.contribution";
import "monaco-editor/esm/vs/basic-languages/dockerfile/dockerfile.contribution";
// The one language service (see above): JSON has no basic-language grammar.
import "monaco-editor/esm/vs/language/json/monaco.contribution";

import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";

/**
 * The ANVIL theme rides *this* chunk, and it is re-exported rather than imported
 * by `monaco.ts` for the same reason everything else here is: `monaco.ts` is on
 * the launch path (the store imports it), so an import of the colour tables
 * there would put them in the entry chunk — a few kilobytes of editor palette
 * parsed before the first pixel by a window that may never open a file. Reached
 * from here they arrive with the editor, on one request, and `loadMonaco` takes
 * the builder off this module as it lands.
 */
export { workbenchThemeData } from "./editorTheme";

export type MonacoApi = typeof monaco;

/**
 * Point Monaco at the workers Vite bundled, and hand the instance to
 * `@monaco-editor/react` so its `<Editor>` uses it instead of fetching a copy
 * from a CDN — which is what `loader` does by default, and which would be a
 * network dependency in a local-first app.
 *
 * Called once, from `loadMonaco`. Both live in this chunk on purpose: nothing
 * on the launch path may so much as name `@monaco-editor/loader`.
 */
export function configureBundle(): MonacoApi {
  (globalThis as { MonacoEnvironment?: monaco.Environment }).MonacoEnvironment = {
    getWorker(_workerId: string, label: string): Worker {
      // Only JSON has a service worker of its own; every other language is a
      // grammar, and the generic editor worker covers what it needs.
      return label === "json" ? new jsonWorker() : new editorWorker();
    },
  };
  loader.config({ monaco });
  return monaco;
}

export default monaco;

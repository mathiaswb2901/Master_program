/**
 * The two ways the editor's theme can be wrong without anything failing.
 *
 * `ui/src/editorTheme.ts` is the one place in the app that hands colours to a
 * third-party API, and it does it with strings on both sides:
 *
 *  1. **A token name `tokens.css` does not define.** `getComputedStyle` answers
 *     `""` for an unknown custom property, so the colour becomes the empty
 *     string, Monaco falls back to its own default, and the editor is quietly a
 *     little bit VS Code again. This is `tokenRefs.test.ts`'s bug — the
 *     `--accent-tint` that painted nothing — wearing a `.ts` extension, and no
 *     stylesheet check can see it because there is no stylesheet.
 *  2. **A colour id Monaco does not register.** `defineTheme` accepts any key
 *     and drops the ones it has no slot for, without a word. A theme can name
 *     `editorSuggestWidget.selectionBackground` (there is no such id — the real
 *     one is `selectedBackground`) and look exactly like a theme that works.
 *
 * Both are checked against the real files rather than a copy of them —
 * `tokens.css` off disk, and Monaco's own `registerColor` calls out of the
 * installed package — which is why this lives under `e2e/` beside
 * `palette.test.ts` and `tokenRefs.test.ts`: files outside `tsc -b`'s program
 * are the ones allowed to touch the filesystem.
 *
 * Scanning `monaco-editor`'s ESM tree for its registered ids costs ~180 ms once,
 * which is the price of never trusting a hand-copied list of them.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { workbenchThemeData } from "../src/editorTheme";

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..");
const TOKENS = path.join(UI_ROOT, "src", "design", "tokens.css");
const SOURCE = path.join(UI_ROOT, "src", "editorTheme.ts");
const MONACO_ESM = path.join(UI_ROOT, "node_modules", "monaco-editor", "esm", "vs");

/**
 * Source with every comment removed — block *and* line.
 *
 * Both halves matter: the module documents the vendor colours it exists to
 * displace (a gold `#FFD700` bracket, `#001188` variables), and a check for
 * "no colour is written down here" that counted those would be a check nobody
 * could keep green while explaining themselves.
 */
const stripComments = (text: string): string =>
  text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");

/** Memoised: each of these is read or walked exactly once per run. */
const source = ((): string => stripComments(fs.readFileSync(SOURCE, "utf-8")))();

const definedTokens = ((): ReadonlySet<string> => {
  const css = fs.readFileSync(TOKENS, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");
  return new Set([...css.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map((match) => match[1]));
})();

const registeredColorIds = ((): ReadonlySet<string> => {
  const ids = new Set<string>();
  const walk = (dir: string): void => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".js")) {
        for (const match of fs.readFileSync(full, "utf-8").matchAll(/registerColor\(\s*'([^']+)'/g)) {
          ids.add(match[1]);
        }
      }
    }
  };
  walk(MONACO_ESM);
  return ids;
})();

/** Colour ids the theme actually emits — the built object, not a reading of it. */
let themeColorIds: string[] = [];

beforeAll(() => {
  // The values are irrelevant to this file: it is about the names on both sides
  // of the call. One stub colour for every token keeps `hexVar` on its normal
  // branch without a DOM.
  vi.stubGlobal("document", { documentElement: {} });
  vi.stubGlobal("getComputedStyle", () => ({ getPropertyValue: () => "#123456" }));
  themeColorIds = Object.keys(workbenchThemeData("dark").colors);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("the editor theme's two string contracts", () => {
  it("finds the files it is checking against", () => {
    // A guard on the guard: a moved directory would make every test below pass
    // by checking nothing at all.
    expect(definedTokens.size).toBeGreaterThan(50);
    expect(registeredColorIds.size).toBeGreaterThan(200);
    expect(themeColorIds.length).toBeGreaterThan(60);
  });

  it("reads only design tokens that exist", () => {
    const referenced = new Set(
      [...source.matchAll(/"(--[a-zA-Z0-9-]+)"/g)].map((match) => match[1]),
    );
    expect(referenced.size, "the theme names no tokens at all").toBeGreaterThan(20);
    expect([...referenced].filter((token) => !definedTokens.has(token))).toEqual([]);
  });

  it("names only colour ids Monaco actually registers", () => {
    const unknown = themeColorIds.filter((id) => !registeredColorIds.has(id));
    expect(unknown, "silently dropped by defineTheme — nothing would look wrong").toEqual([]);
  });

  it("writes no colour of its own", () => {
    // The failure this whole design exists to refuse: a parallel hex palette in
    // TypeScript that starts life agreeing with `tokens.css` and then drifts,
    // in the one surface `tokenRefs.test.ts` and `palette.test.ts` cannot see.
    expect(source.match(/#[0-9A-Fa-f]{3,8}\b/g) ?? [], "a colour outside tokens.css").toEqual([]);
  });

  it("still refuses to inherit, in the file and not only in the built object", () => {
    // Stated at the source as well: `inherit: true` is a one-word edit that puts
    // every built-in rule back at once and changes no other test's shape.
    expect(source).toMatch(/inherit:\s*false/);
  });
});

/**
 * The palette weighs ~14 kB of tables, and the rule it has to keep is
 * `CLAUDE.md`'s: nothing that is not needed to paint is statically reachable
 * from `main.tsx`. `perf/bundle.spec.ts` guards the *symptom* — Monaco's own
 * modules in the entry chunk — but rollup attributes our files as one bucket
 * called `src`, so it cannot see 14 kB of editor palette arriving there. This
 * can: it names, in the source, the two modules allowed to import the palette,
 * and why each of them is off the launch path.
 */
describe("the palette stays on the editor's chunk", () => {
  const ALLOWED: Readonly<Record<string, string>> = {
    "src/monacoBundle.ts": "the Monaco chunk itself — reached only by one dynamic import()",
    "src/panels/CodeEditor.tsx": "React.lazy, and only after loadMonaco() has resolved",
  };

  /**
   * A static `import` or `export … from "…/editorTheme"` beginning a line.
   *
   * `export … from` counts, and has to: that is exactly how `monacoBundle.ts`
   * hands the builder on. `import type` does not — the compiler erases it — and
   * neither does `typeof import("./editorTheme")` in a type position, which is
   * how `monaco.ts` holds the builder's signature without an edge to it.
   */
  const STATIC_EDGE = /^\s*(?:import|export)\s+(?!type\b)[^;]*?["'][^"']*\/editorTheme["']/m;

  /** Every `.ts`/`.tsx` under `src/` carrying such an edge. */
  function importers(): string[] {
    const found: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
          if (STATIC_EDGE.test(stripComments(fs.readFileSync(full, "utf-8")))) {
            found.push(path.relative(UI_ROOT, full).replace(/\\/g, "/"));
          }
        }
      }
    };
    walk(path.join(UI_ROOT, "src"));
    return found.sort();
  }

  it("is imported only by modules that are themselves off the launch path", () => {
    expect(importers()).toEqual(Object.keys(ALLOWED).sort());
  });

  it("is not named at runtime by the module the store pulls in", () => {
    // `monaco.ts` is on the launch path (`store.ts` imports it for
    // `setModelContent`), so it takes the builder off the bundle instead. A
    // plain import here would put the tables back in the entry chunk without a
    // single byte of Monaco moving, which is the failure `bundle.spec.ts`
    // cannot see.
    const monaco = stripComments(fs.readFileSync(path.join(UI_ROOT, "src", "monaco.ts"), "utf-8"));
    expect(monaco).not.toMatch(STATIC_EDGE);
    expect(monaco, "the builder is taken off the bundle, by reference").toMatch(
      /buildTheme = bundle\.workbenchThemeData/,
    );
  });
});

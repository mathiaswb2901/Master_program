/**
 * The one invariant a hand-picked Monaco build can silently break.
 *
 * `languageForPath` claims a language id for an extension; `monacoBundle.ts`
 * imports the contribution that registers it. Nothing connects the two at
 * compile time, and the failure is quiet in the worst way — a `.rs` file opens
 * as plain text, with no error anywhere, because `defaultLanguage="rust"` for a
 * language nobody registered is not an error to Monaco. So the two lists are
 * checked against each other here, in both directions:
 *
 * * every id the table can return has a contribution behind it, and
 * * every contribution imported is reachable from the table — otherwise it is
 *   bytes on the first-open path that no file can ask for, which is exactly
 *   what dropping the barrel was about.
 *
 * The bundle is read as text rather than imported: importing it would pull
 * 3.5 MB of browser-only code into a node test run to learn which lines it
 * starts with.
 */

import { describe, expect, it } from "vitest";

import bundleSource from "./monacoBundle.ts?raw";
import { languageForPath, SHIPPED_LANGUAGES } from "./monaco";

/** Contribution directories imported by the bundle, e.g. `python`, `cpp`. */
const contributions = new Set(
  [...bundleSource.matchAll(/basic-languages\/([\w-]+)\/[\w-]+\.contribution/g)].map(
    (match) => match[1],
  ),
);

/**
 * The two places a language id does not equal its contribution directory.
 * Monaco's `cpp` contribution registers `c` as well; JSON has no
 * basic-language grammar at all and comes from its language service.
 */
const EXTRA_IDS: Record<string, readonly string[]> = { cpp: ["c"] };
const SERVICE_IDS = /vs\/language\/json\/monaco\.contribution/.test(bundleSource) ? ["json"] : [];

const registered = new Set([
  ...contributions,
  ...[...contributions].flatMap((dir) => EXTRA_IDS[dir] ?? []),
  ...SERVICE_IDS,
]);

describe("the shipped Monaco languages", () => {
  it("has a contribution for every language the extension table claims", () => {
    const missing = SHIPPED_LANGUAGES.filter((id) => !registered.has(id));
    expect(missing, "imported in monacoBundle.ts, or these files open as plain text").toEqual([]);
  });

  it("imports no contribution the extension table cannot reach", () => {
    const unreachable = [...registered].filter((id) => !SHIPPED_LANGUAGES.includes(id));
    expect(unreachable, "dead weight on the first-open path").toEqual([]);
  });

  it("does not claim the languages the barrel used to register", () => {
    // A spot check that the barrel really is gone: these are basic-languages
    // contributions `import * as monaco from "monaco-editor"` brought along.
    for (const id of ["abap", "apex", "cypher", "objective-c", "solidity", "wgsl"]) {
      expect(registered.has(id), `${id} is back`).toBe(false);
    }
  });

  it("maps the extensions this workspace is made of", () => {
    expect(languageForPath("server/src/app.py")).toBe("python");
    expect(languageForPath("ui/src/store.ts")).toBe("typescript");
    expect(languageForPath(".workbench/layouts.json")).toBe("json");
    expect(languageForPath("ROADMAP.md")).toBe("markdown");
    expect(languageForPath("pyproject.toml")).toBe("ini");
    expect(languageForPath("scripts/run.ps1")).toBe("powershell");
    expect(languageForPath("Dockerfile")).toBe("dockerfile");
    expect(languageForPath("LICENSE")).toBe("plaintext");
  });
});

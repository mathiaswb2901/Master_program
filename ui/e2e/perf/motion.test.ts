/**
 * Motion conformance — the half that needs no browser (DESIGN.md §5).
 *
 * Two things a review cannot be trusted to catch, because both are one
 * plausible line in one stylesheet:
 *
 *  - **Animating a property that triggers layout.** `width`, `height`, `top`,
 *    `left` — the single most common way an app becomes janky, and invisible
 *    until someone measures a frame. The allowlist here is DESIGN.md §5's, and
 *    it covers every stylesheet under `ui/src/`, so a new one is in scope the
 *    day it lands rather than the day someone remembers to add it.
 *  - **A hover that eases in.** A control that fades *up* under the cursor
 *    feels laggy however short the fade, so §5 makes hover asymmetric: instant
 *    in, eased out. That is a rule about a duration, and durations drift.
 *
 * It lives here, next to the perf lane's frame budgets, because it is the same
 * lane's job — but it is a vitest test rather than a Playwright one so it runs
 * on `npm run test` in seconds. `motion.spec.ts` holds the half that needs the
 * production build and a real browser. Both are outside the `tsc -b` program,
 * which is what lets them read files off disk (`e2e/perf/workspace.test.ts` is
 * the precedent).
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { springTokenBlock } from "../../src/design/springs";
import { ANIMATABLE, motionDeclarations, rules } from "./css";

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..");
const STYLE_ROOT = path.join(UI_ROOT, "src");

/** Every stylesheet this repo writes. Globbed, not listed: a new one is covered
 * the day it lands. */
function ownStylesheets(): { file: string; css: string }[] {
  const found: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".css")) found.push(full);
    }
  };
  walk(STYLE_ROOT);
  return found.map((file) => ({
    file: path.relative(UI_ROOT, file).replace(/\\/g, "/"),
    css: fs.readFileSync(file, "utf-8"),
  }));
}

/**
 * `:root`'s custom properties, so a test can ask what a token *resolves to*
 * rather than what its name suggests.
 *
 * `rules()` reports blocks in source order and descends into at-rules
 * afterwards, so the first `:root` is the top-level one — not the override
 * inside the reduced-motion query.
 */
function rootTokens(): Map<string, string> {
  const css = fs.readFileSync(path.join(STYLE_ROOT, "design", "tokens.css"), "utf-8");
  const root = rules(css).find((rule) => rule.selector === ":root");
  expect(root, "tokens.css must declare its tokens on :root").toBeDefined();
  const out = new Map<string, string>();
  for (const declaration of String(root?.body).split(";")) {
    const colon = declaration.indexOf(":");
    if (colon < 0) continue;
    const name = declaration.slice(0, colon).trim();
    if (name.startsWith("--")) out.set(name, declaration.slice(colon + 1).trim());
  }
  return out;
}

/** Substitute `var(--x)` until nothing is left to substitute. */
function resolve(value: string, tokens: Map<string, string>): string {
  let out = value;
  for (let i = 0; i < 8 && out.includes("var("); i++) {
    out = out.replace(/var\(\s*(--[\w-]+)\s*(?:,[^()]*)?\)/g, (whole, name: string) =>
      tokens.get(name) ?? whole,
    );
  }
  return out;
}

/** The tint channel's properties: paint, no geometry. `transform` is the other
 * channel and `opacity` belongs to tint but is what an entrance shares with it,
 * so neither is in here — this set is "colour and nothing else". */
const COLOUR: ReadonlySet<string> = new Set([
  "background-color",
  "border-color",
  "color",
  "outline-color",
]);

interface ColourTransition {
  file: string;
  selector: string;
  /** The declaration's value with every token substituted. */
  resolved: string;
}

/** Every transition in `ui/src/` that animates colour and nothing else. */
function colourTransitions(): ColourTransition[] {
  const tokens = rootTokens();
  const out: ColourTransition[] = [];
  for (const { file, css } of ownStylesheets()) {
    for (const declaration of motionDeclarations(css)) {
      if (declaration.properties.length === 0) continue;
      if (!declaration.properties.every((property) => COLOUR.has(property))) continue;
      out.push({ file, selector: declaration.selector, resolved: resolve(declaration.raw, tokens) });
    }
  }
  return out;
}

describe("the two channels stay separate (DESIGN.md §5.1.3)", () => {
  /**
   * A `linear()` polyline is a sampled spring and nothing else in this app is
   * one, so "the value resolved to a `linear()`" is exactly "this is on the
   * travel channel". Checking the resolved value rather than the token name is
   * the whole point: `--ease-standard` moved two other lanes' stylesheets onto
   * the spring without any of them changing a character, and a test that asked
   * which *properties* were animated could not see it.
   */
  it("never eases a colour on a spring", () => {
    const offenders = colourTransitions()
      .filter((entry) => entry.resolved.includes("linear("))
      .map((entry) => `${entry.file}  ${entry.selector}`);
    expect(offenders).toEqual([]);
  });

  it("has colour transitions to check, or the rule above is vacuous", () => {
    expect(colourTransitions().length).toBeGreaterThanOrEqual(5);
  });

  it("keeps the deprecated aliases on the channel their consumers use", () => {
    // Stated at the token, not only at its consumers: those live in other
    // lanes' files and may move, and this alias must not follow them.
    const tokens = rootTokens();
    expect(resolve("var(--ease-standard)", tokens)).toBe(resolve("var(--ease-tint)", tokens));
  });
});

describe("what Workbench animates", () => {
  it("finds the stylesheets at all", () => {
    // A glob that silently matched nothing would make every test below pass.
    expect(ownStylesheets().length).toBeGreaterThanOrEqual(10);
  });

  it("is transform, opacity and colour — nothing that triggers layout", () => {
    const offenders: string[] = [];
    for (const { file, css } of ownStylesheets()) {
      for (const declaration of motionDeclarations(css)) {
        for (const property of declaration.properties) {
          if (property === "none" || ANIMATABLE.has(property)) continue;
          offenders.push(`${file}  ${declaration.selector} { transition: ${declaration.raw} }`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("is never `all` — a shorthand that animates whatever a later edit adds", () => {
    const offenders: string[] = [];
    for (const { file, css } of ownStylesheets()) {
      for (const declaration of motionDeclarations(css)) {
        if (declaration.properties.includes("all")) offenders.push(`${file}  ${declaration.selector}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("keeps every keyframe on the same allowlist", () => {
    const offenders: string[] = [];
    for (const { file, css } of ownStylesheets()) {
      for (const { selector, body } of rules(css)) {
        // `rules()` flattens at-rules, so a keyframe step arrives as `from`,
        // `to` or `42%` — with the properties it actually animates in its body.
        if (!/^(from|to|[\d.]+%)$/.test(selector)) continue;
        for (const declaration of body.split(";")) {
          const property = declaration.split(":")[0].trim().toLowerCase();
          if (property === "" || ANIMATABLE.has(property)) continue;
          offenders.push(`${file}  ${selector} { ${declaration.trim()} }`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("declares no static will-change — a permanent layer is not an animation", () => {
    const offenders: string[] = [];
    for (const { file, css } of ownStylesheets()) {
      if (/will-change\s*:/.test(css.replace(/\/\*[\s\S]*?\*\//g, ""))) offenders.push(file);
    }
    expect(offenders).toEqual([]);
  });
});

describe("hover", () => {
  it("costs nothing on the way in", () => {
    const offenders: string[] = [];
    for (const { file, css } of ownStylesheets()) {
      for (const { selector, body } of rules(css)) {
        if (!selector.includes(":hover")) continue;
        for (const declaration of body.split(";")) {
          const colon = declaration.indexOf(":");
          if (colon < 0) continue;
          const property = declaration.slice(0, colon).trim().toLowerCase();
          const value = declaration.slice(colon + 1).trim();
          if (property !== "transition" && property !== "transition-duration") continue;
          // Every duration in the value must be zero: the pointer arriving is
          // answered on the frame it arrives (DESIGN.md §5).
          const durations = value.match(/(^|[\s,])[\d.]+m?s\b/g) ?? [];
          const nonZero = durations.filter((d) => Number.parseFloat(d) !== 0);
          if (durations.length === 0 || nonZero.length > 0) {
            offenders.push(`${file}  ${selector} { ${declaration.trim()} }`);
          }
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("is asymmetric somewhere, or the rule above is vacuous", () => {
    const instant = ownStylesheets().flatMap(({ css }) =>
      rules(css).filter(
        (rule) => rule.selector.includes(":hover") && /transition-duration\s*:\s*0/.test(rule.body),
      ),
    );
    expect(instant.length).toBeGreaterThanOrEqual(5);
  });
});

describe("reduced motion", () => {
  const reduced = (): string => {
    const tokens = fs.readFileSync(path.join(STYLE_ROOT, "design", "tokens.css"), "utf-8");
    const block = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*)\n\}/.exec(tokens);
    expect(block, "tokens.css must carry the reduced-motion rule").not.toBeNull();
    return String(block?.[1]);
  };

  it("zeroes every travel distance", () => {
    const css = reduced();
    for (const declaration of [
      "--motion-rise: 0px",
      "--motion-lift: 0px",
      "--motion-scale-in: 1",
      "--motion-zoom-in: 1",
    ]) {
      expect(css).toContain(declaration);
    }
  });

  it("zeroes every transform transition", () => {
    const css = reduced();
    for (const token of ["--motion-move-snap", "--motion-move"]) {
      expect(css).toMatch(new RegExp(`${token}:\\s*0s`));
    }
  });

  it("leaves the tint channel alone", () => {
    // The crude version — every duration to 1 ms — takes the colour feedback
    // with it. These two are what says this one does not.
    const css = reduced();
    expect(css).not.toContain("--motion-tint-ms");
    expect(css).not.toContain("--motion-exit-ms");
  });
});

it("tokens.css carries exactly what the spring derivation produces", () => {
  const tokens = fs
    .readFileSync(path.join(STYLE_ROOT, "design", "tokens.css"), "utf-8")
    .replace(/\r\n/g, "\n");
  const block = springTokenBlock();
  expect(
    tokens.includes(block),
    `tokens.css is out of date with src/design/springs.ts. Replace the generated block with:\n\n${block}\n`,
  ).toBe(true);
});

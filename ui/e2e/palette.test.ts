/**
 * ANVIL palette conformance — DESIGN.md §2, made into a gate.
 *
 * The identity is a set of *numbers*, not a set of hex codes: a value ramp with
 * a stated step, hairlines above the threshold at which an edge is perceived,
 * text above the WCAG floor at 11px, zero chroma in every neutral, and exactly
 * one amber that nothing else comes near. A palette drifts one token at a time
 * and every one of those steps is plausible in review — this is what makes them
 * fail instead.
 *
 * Every figure quoted in DESIGN.md §2 and §7 is asserted here, so the document
 * and the stylesheet cannot disagree without the build saying so. It lives
 * under `e2e/` for the same reason `perf/motion.test.ts` does: files outside the
 * `tsc -b` program may read from disk, and reading `tokens.css` itself — rather
 * than a copy of its values — is the whole point.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { rules } from "./perf/css";

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..");
const TOKENS = path.join(UI_ROOT, "src", "design", "tokens.css");

// ---- colour maths -----------------------------------------------------------

type Rgb = [number, number, number];

function parseHex(hex: string): Rgb {
  const h = hex.trim().replace("#", "");
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
  return [
    parseInt(full.slice(0, 2), 16),
    parseInt(full.slice(2, 4), 16),
    parseInt(full.slice(4, 6), 16),
  ];
}

const linear = (channel: number): number => {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
};

/** WCAG relative luminance. */
function luminance(hex: string): number {
  const [r, g, b] = parseHex(hex).map(linear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2.x contrast ratio, the figure every table in DESIGN.md §2 quotes. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** CIE L*a*b* under D65 — L* is the ramp's unit, C* is how neutral a grey is. */
function lab(hex: string): [number, number, number] {
  const [r, g, b] = parseHex(hex).map(linear);
  const x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047;
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
  const z = (0.0193339 * r + 0.119192 * g + 0.9503041 * b) / 1.08883;
  const f = (t: number): number => (t > (6 / 29) ** 3 ? Math.cbrt(t) : t / (3 * (6 / 29) ** 2) + 4 / 29);
  const [fx, fy, fz] = [f(x), f(y), f(z)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

const lightness = (hex: string): number => lab(hex)[0];
const chroma = (hex: string): number => Math.hypot(lab(hex)[1], lab(hex)[2]);

/** CIE76 colour difference — "are these two the same colour at a glance". */
function deltaE(a: string, b: string): number {
  const [la, aa, ba] = lab(a);
  const [lb, ab, bb] = lab(b);
  return Math.hypot(la - lb, aa - ab, ba - bb);
}

// ---- the token file ---------------------------------------------------------

/** Declarations of one top-level block, by name. */
function block(selector: string): Map<string, string> {
  const css = fs.readFileSync(TOKENS, "utf-8");
  const found = rules(css).find((rule) => rule.selector === selector);
  expect(found, `tokens.css must declare a ${selector} block`).toBeDefined();
  const out = new Map<string, string>();
  for (const declaration of String(found?.body).split(";")) {
    const colon = declaration.indexOf(":");
    if (colon < 0) continue;
    const name = declaration.slice(0, colon).trim();
    if (name.startsWith("--")) out.set(name, declaration.slice(colon + 1).trim());
  }
  return out;
}

const DARK = block(":root");
const LIGHT = new Map([...DARK, ...block('[data-theme="light"]')]);

/** A token's value, required to be a plain hex — the tables below are about
 * opaque colours, and a wash is checked separately by compositing it. */
function hex(theme: Map<string, string>, token: string): string {
  const value = theme.get(token);
  expect(value, `${token} must exist`).toBeDefined();
  expect(String(value), `${token} must be a hex colour`).toMatch(/^#[0-9A-Fa-f]{6}$/);
  return String(value);
}

/** `rgba(r, g, b, a)` composited over an opaque backdrop. */
function over(wash: string, backdrop: string): string {
  const m = /^rgba\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)[,\s/]+([\d.]+)\s*\)$/.exec(wash.trim());
  expect(m, `expected an rgba() wash, got ${wash}`).not.toBeNull();
  const [, r, g, b, a] = m as RegExpExecArray;
  const base = parseHex(backdrop);
  const mix = [Number(r), Number(g), Number(b)].map((channel, i) =>
    Math.round(channel * Number(a) + base[i] * (1 - Number(a))),
  );
  return `#${mix.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

const THEMES: [string, Map<string, string>][] = [
  ["dark", DARK],
  ["light", LIGHT],
];

/** Every surface a chrome colour is ever drawn on, deepest well first. */
const RAMP = [
  "--surface-terminal",
  "--surface-code",
  "--surface-panel",
  "--surface-elevated",
  "--surface-app",
  "--surface-overlay",
];

const NEUTRALS = [
  ...RAMP,
  "--surface-paper",
  "--surface-paper-surround",
  "--text-primary",
  "--text-secondary",
  "--text-tertiary",
  "--text-disabled",
  "--text-on-accent",
  "--text-on-paper",
  "--border-subtle",
  "--border-default",
  "--border-strong",
  "--border-paper-rim",
  "--agent-idle",
  "--ansi-black",
  "--ansi-white",
  "--ansi-bright-black",
  "--ansi-bright-white",
];

// ---- the ramp ---------------------------------------------------------------

describe("the value ramp", () => {
  it.each(THEMES)("%s steps monotonically away from its ground", (_name, theme) => {
    const steps = RAMP.map((token) => lightness(hex(theme, token)));
    for (let i = 1; i < steps.length; i++) {
      // Dark climbs from #000000, light descends from #FFFFFF; both are "one
      // step further from the content", which is the rule DESIGN.md §2.1 states.
      const delta = Math.abs(steps[i] - steps[i - 1]);
      expect(Math.sign(steps[i] - steps[i - 1]), `${RAMP[i]} must continue the ramp`).toBe(
        Math.sign(steps[1] - steps[0]),
      );
      expect(delta, `${RAMP[i - 1]} -> ${RAMP[i]} must be a step you can see`).toBeGreaterThan(3.5);
      expect(delta, `${RAMP[i - 1]} -> ${RAMP[i]} must not be a jump`).toBeLessThan(7);
    }
  });

  it("dark spans 29.7 L* end to end and light 20.1", () => {
    const span = (theme: Map<string, string>): number =>
      Math.abs(lightness(hex(theme, RAMP[0])) - lightness(hex(theme, RAMP[RAMP.length - 1])));
    expect(span(DARK)).toBeCloseTo(29.7, 1);
    expect(span(LIGHT)).toBeCloseTo(20.1, 1);
  });

  it("puts the app chrome LIGHTER than the panel it frames, in dark", () => {
    // The inversion is the identity. If this ever flips back, the window is a
    // graphite editor again and every surface decision downstream is wrong.
    expect(lightness(hex(DARK, "--surface-app"))).toBeGreaterThan(
      lightness(hex(DARK, "--surface-panel")),
    );
    expect(lightness(hex(LIGHT, "--surface-app"))).toBeLessThan(
      lightness(hex(LIGHT, "--surface-panel")),
    );
  });
});

describe("neutrals are neutral", () => {
  it.each(THEMES)("%s carries zero chroma in every grey", (_name, theme) => {
    for (const token of NEUTRALS) {
      // 0.01 rather than exactly 0: the sRGB->XYZ matrix is rounded, so an
      // equal-channel grey lands ~1e-5 off the D65 white point. Any real tint
      // is orders of magnitude above that — the old graphite ramp measured
      // C* 2.2 to 4.1.
      expect(chroma(hex(theme, token)), `${token} must be achromatic`).toBeLessThan(0.01);
    }
  });
});

// ---- contrast ---------------------------------------------------------------

describe("text clears WCAG on every surface it is drawn on", () => {
  // --text-disabled is deliberately absent: WCAG 1.4.3 exempts disabled
  // controls, and a disabled row that reads as enabled is the worse failure.
  const TEXT = ["--text-primary", "--text-secondary", "--text-tertiary"];

  it.each(THEMES)("%s: every text token clears 4.5:1 on every surface", (_name, theme) => {
    for (const surface of RAMP) {
      for (const token of TEXT) {
        const ratio = contrast(hex(theme, token), hex(theme, surface));
        expect(ratio, `${token} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("names its own worst pair, so a claim of '>= 5:1' cannot drift", () => {
    // Every other pair in the system is >= 5:1. The exception is one cell and
    // it is stated in DESIGN.md §7 rather than rounded away: tertiary metadata
    // inside the QuickBar, which is the lightest chrome surface in dark.
    const pairs = THEMES.flatMap(([name, theme]) =>
      RAMP.flatMap((surface) =>
        TEXT.map((token) => ({
          where: `${name} ${token} on ${surface}`,
          ratio: contrast(hex(theme, token), hex(theme, surface)),
        })),
      ),
    );
    const worst = pairs.reduce((a, b) => (a.ratio <= b.ratio ? a : b));
    expect(worst.where).toBe("dark --text-tertiary on --surface-overlay");
    expect(worst.ratio).toBeCloseTo(4.55, 1);
    const others = pairs.filter((p) => p.where !== worst.where);
    expect(Math.min(...others.map((p) => p.ratio))).toBeGreaterThanOrEqual(5);
  });

  it.each(THEMES)("%s: --text-tertiary is the one that had to move", (_name, theme) => {
    // The measured failure this palette exists to fix: 4.24:1 under 11px text.
    expect(contrast(hex(theme, "--text-tertiary"), hex(theme, "--surface-panel"))).toBeCloseTo(
      7.85,
      1,
    );
  });

  it("publishes exactly the figures DESIGN.md §7 quotes", () => {
    // Every row of the §7 table, so the document and the stylesheet cannot
    // disagree. A figure that changes here has to be edited there.
    const rows: [Map<string, string>, string, string, number][] = [
      [DARK, "--text-primary", "--surface-panel", 14.94],
      [DARK, "--text-secondary", "--surface-panel", 11.21],
      [DARK, "--text-tertiary", "--surface-panel", 7.86],
      [DARK, "--text-tertiary", "--surface-app", 5.57],
      [DARK, "--text-tertiary", "--surface-overlay", 4.55],
      [DARK, "--text-disabled", "--surface-panel", 4.42],
      [DARK, "--text-primary", "--surface-terminal", 19.26],
      [DARK, "--accent", "--surface-panel", 9.76],
      [DARK, "--accent", "--surface-app", 6.92],
      [LIGHT, "--text-primary", "--surface-panel", 14.9],
      [LIGHT, "--text-secondary", "--surface-panel", 11.24],
      [LIGHT, "--text-tertiary", "--surface-panel", 7.83],
      [LIGHT, "--text-tertiary", "--surface-app", 6.28],
      [LIGHT, "--text-tertiary", "--surface-overlay", 5.61],
      [LIGHT, "--text-disabled", "--surface-panel", 4.41],
      [LIGHT, "--text-primary", "--surface-terminal", 18.26],
      [LIGHT, "--accent", "--surface-panel", 4.67],
      [LIGHT, "--accent", "--surface-app", 3.75],
    ];
    for (const [theme, token, surface, expected] of rows) {
      expect(contrast(hex(theme, token), hex(theme, surface)), `${token} on ${surface}`)
        .toBeCloseTo(expected, 1);
    }
    for (const [, theme] of THEMES) {
      expect(contrast(hex(theme, "--text-on-accent"), hex(theme, "--accent-fill"))).toBeCloseTo(
        11.04,
        1,
      );
    }
  });
});

describe("hairlines carry structure, so they must be visible", () => {
  // DESIGN.md §1.4 gives every piece of structure to a 1px border and forbids
  // shadows as the fallback. 2.5:1 is where a 1px edge stops being a guess —
  // the old --border-subtle was 1.17:1 on panel, which is why no panel read as
  // an object.
  it.each(THEMES)("%s: --border-subtle is 2.51:1 on --surface-panel", (_name, theme) => {
    expect(contrast(hex(theme, "--border-subtle"), hex(theme, "--surface-panel"))).toBeCloseTo(
      2.51,
      1,
    );
  });

  it.each(THEMES)("%s: borders escalate, and the rim frames paper", (_name, theme) => {
    const panel = hex(theme, "--surface-panel");
    const subtle = contrast(hex(theme, "--border-subtle"), panel);
    const dflt = contrast(hex(theme, "--border-default"), panel);
    const strong = contrast(hex(theme, "--border-strong"), panel);
    expect(dflt).toBeGreaterThan(subtle);
    expect(strong).toBeGreaterThan(dflt);
    // A rim that cannot be seen against the page is not a rim (§6.1).
    expect(
      contrast(hex(theme, "--border-paper-rim"), hex(theme, "--surface-paper")),
      "paper rim vs the page",
    ).toBeGreaterThanOrEqual(2);
    expect(
      contrast(hex(theme, "--border-paper-rim"), hex(theme, "--surface-paper-surround")),
      "paper rim vs the mat",
    ).toBeGreaterThanOrEqual(1.5);
  });

  it.each(THEMES)("%s: the mat is far enough from paper to read as a mat", (_name, theme) => {
    expect(
      contrast(hex(theme, "--surface-paper"), hex(theme, "--surface-paper-surround")),
    ).toBeGreaterThanOrEqual(2.4);
  });

  it.each(THEMES)("%s: the mat carries the app's text pair, not paper's", (_name, theme) => {
    // The mat is mid-grey in both themes, so it is the one surface where the
    // paper pair fails: `--text-on-paper` on the dark mat measures 2.67:1. The
    // document tab strip is real 12px text and must clear 4.5:1 (§7).
    const mat = hex(theme, "--surface-paper-surround");
    expect(contrast(hex(theme, "--text-primary"), mat)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(hex(theme, "--text-secondary"), mat)).toBeGreaterThanOrEqual(4.5);
  });
});

// ---- the one amber ----------------------------------------------------------

describe("exactly one amber, and it is unmistakable", () => {
  it("is the same hot amber in dark and the same hue in light", () => {
    // Same family, different job: dark marks with the hot one, light marks with
    // a deep one because the hot amber is 1.33:1 on a light panel.
    expect(hex(DARK, "--accent")).toBe("#FBBF24");
    expect(deltaE(hex(LIGHT, "--accent"), hex(DARK, "--accent"))).toBeGreaterThan(20);
    const hue = (h: string): number => {
      const [, a, b] = lab(h);
      return (Math.atan2(b, a) * 180) / Math.PI;
    };
    expect(
      Math.abs(hue(hex(LIGHT, "--accent")) - hue(hex(DARK, "--accent"))),
      "light's amber must be the same hue, not another colour",
    ).toBeLessThan(8);
  });

  it("the fill is theme-independent and carries near-black text", () => {
    for (const token of ["--accent-fill", "--accent-fill-hover", "--accent-fill-active"]) {
      expect(hex(LIGHT, token), `${token} is one value for both themes`).toBe(hex(DARK, token));
    }
    for (const [, theme] of THEMES) {
      expect(
        contrast(hex(theme, "--text-on-accent"), hex(theme, "--accent-fill")),
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(THEMES)("%s: --accent works as text and as a 2px rule", (_name, theme) => {
    // Text on the surfaces it is actually drawn on...
    for (const surface of ["--surface-panel", "--surface-code", "--surface-terminal"]) {
      expect(
        contrast(hex(theme, "--accent"), hex(theme, surface)),
        `--accent on ${surface}`,
      ).toBeGreaterThanOrEqual(4.5);
    }
    // ...and as the live rule, which sits on chrome and only has to be a UI
    // indicator (WCAG 1.4.11, 3:1).
    for (const surface of ["--surface-app", "--surface-overlay"]) {
      expect(
        contrast(hex(theme, "--accent"), hex(theme, surface)),
        `--accent rule on ${surface}`,
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it.each(THEMES)("%s: the amber wash is visible and holds primary text", (_name, theme) => {
    for (const token of ["--accent-muted", "--surface-selected"]) {
      const wash = String(theme.get(token));
      const on = over(wash, hex(theme, "--surface-panel"));
      expect(contrast(on, hex(theme, "--surface-panel")), `${token} vs the row under it`)
        .toBeGreaterThanOrEqual(1.1);
      expect(contrast(hex(theme, "--text-primary"), on), `--text-primary on ${token}`)
        .toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(THEMES)("%s: no ANSI slot comes within deltaE 25 of the accent", (_name, theme) => {
    // Otherwise a yellow `WARN` in the terminal reads as the focus rule, and
    // amber has stopped meaning "here" — the failure this floor exists for.
    for (const [name, value] of theme) {
      if (!name.startsWith("--ansi-")) continue;
      expect(deltaE(value, hex(theme, "--accent")), `${name} vs --accent`).toBeGreaterThan(25);
    }
  });

  it.each(THEMES)("%s: no semantic colour is mistakable for the accent", (_name, theme) => {
    for (const token of ["--success", "--error", "--info", "--agent-attention", "--agent-idle"]) {
      expect(deltaE(hex(theme, token), hex(theme, "--accent")), token).toBeGreaterThan(25);
    }
    // --warn is the near neighbour by construction (orange beside amber). It
    // still has to be a different colour at a glance, and it is rare.
    expect(deltaE(hex(theme, "--warn"), hex(theme, "--accent"))).toBeGreaterThan(25);
  });
});

// ---- the terminal -----------------------------------------------------------

describe("the ANSI ramp reads on its own ground", () => {
  const SLOTS = [
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "bright-red",
    "bright-green",
    "bright-yellow",
    "bright-blue",
    "bright-magenta",
    "bright-cyan",
    "bright-white",
  ];

  it.each(THEMES)("%s: every slot but `black` is legible in Monaco", (_name, theme) => {
    // Monaco paints on --surface-code and draws its syntax from these tokens,
    // so "legible" is a contrast floor, not a taste question. `black` is the
    // dim slot — it is a background and a rule colour, never body text.
    const codeSurface = hex(theme, "--surface-code");
    for (const slot of SLOTS.filter((s) => s !== "black")) {
      expect(
        contrast(hex(theme, `--ansi-${slot}`), codeSurface),
        `--ansi-${slot} on --surface-code`,
      ).toBeGreaterThanOrEqual(4.5);
    }
    // Comments are text too, and `bright-black` is what Monaco maps them to.
    expect(contrast(hex(theme, "--ansi-bright-black"), codeSurface)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(THEMES)("%s: `black` is still visible on the terminal ground", (_name, theme) => {
    // On #000000 the old #2A2E37 was gone. A dim slot must at least separate.
    expect(
      contrast(hex(theme, "--ansi-black"), hex(theme, "--surface-terminal")),
    ).toBeGreaterThanOrEqual(2);
  });

  it.each(THEMES)("%s: brights are brighter than their normals", (_name, theme) => {
    const direction = Math.sign(
      lightness(hex(theme, "--text-primary")) - lightness(hex(theme, "--surface-panel")),
    );
    for (const slot of ["red", "green", "yellow", "blue", "magenta", "cyan"]) {
      const normal = lightness(hex(theme, `--ansi-${slot}`));
      const bright = lightness(hex(theme, `--ansi-bright-${slot}`));
      expect(Math.sign(bright - normal), `--ansi-bright-${slot}`).toBe(direction);
    }
  });
});
